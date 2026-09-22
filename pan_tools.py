# -*- coding: utf-8 -*-
r"""
pan_tools.py —— 百度网盘（应用沙盒）文件操作库
=============================================

定位：给「分批上传工具」补上网盘的**整理 / 批量改名 / 批量删除**能力。

为什么只做沙盒：百度开放平台 2026-06-03 之后新建的应用，filemanager 等接口
只能操作 `/apps/<应用名>/` 沙盒目录，做不了全盘；全盘整理需要走 alist（NAS）。
所以这里所有操作都**强制校验路径必须落在沙盒根目录内**，越界直接拒绝。

能力：
  list_dir(path, recursive)        递归列目录
  mkdir(path)                      建目录（自动建父目录）
  rename_batch(ops)                批量改名（filemanager rename）
  move_batch(ops)                  批量移动
  delete_batch(paths)              批量删除（危险，调用方必须二次确认）

以及「计划」构建（先出计划、预览、再执行，绝不直接改）：
  build_rename_plan(files, mode, params)
  build_organize_plan(files, by, dest)
  build_delete_plan(files, filters)

改名四模式：replace（查找替换）/ affix（加前后缀）/ serial（序号）
          / regex（正则捕获组）/ clean（去广告与括号）

依赖：requests；Token 复用 upload_baidu.py 的 BaiduAuth（token.json）。
"""

import re
import time
import unicodedata
from pathlib import PurePosixPath

import requests

FILEMANAGER = "https://pan.baidu.com/rest/2.0/xpan/file?method=filemanager"
LIST_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=list"
CREATE_URL = "https://pan.baidu.com/rest/2.0/xpan/file?method=create"

# 百度 filemanager 单次最多提交多少个（留余量，官方上限 1000）
BATCH_LIMIT = 100


class PanError(Exception):
    pass


class PanFiles:
    """百度网盘沙盒内的文件操作封装"""

    def __init__(self, access_token: str, sandbox: str, timeout: int = 30):
        self.token = access_token
        self.sandbox = sandbox.rstrip("/")     # 例如 /apps/baidu_uploader
        self.timeout = timeout

    # ---------- 安全校验 ----------
    def check_path(self, path: str) -> str:
        """所有待操作路径必须落在沙盒内，越界直接抛异常（防误删整盘）"""
        p = (path or "").strip()
        if not p.startswith("/"):
            p = "/" + p
        if p != self.sandbox and not p.startswith(self.sandbox + "/"):
            raise PanError(f"路径越界，只允许操作沙盒目录 {self.sandbox} 内的文件：{path}")
        return p

    # ---------- 基础请求 ----------
    def _get(self, url, params, retry=2):
        params = {**params, "access_token": self.token}
        for i in range(retry + 1):
            try:
                r = requests.get(url, params=params, timeout=self.timeout).json()
            except Exception as e:
                if i >= retry:
                    raise PanError(f"网络异常: {e}")
                time.sleep(3)
                continue
            errno = r.get("errno", 0)
            if errno == 31034 and i < retry:
                time.sleep(20)
                continue
            return r
        return r

    def _post(self, url, params, data=None, retry=2):
        params = {**params, "access_token": self.token}
        for i in range(retry + 1):
            try:
                r = requests.post(url, params=params, data=data,
                                  timeout=self.timeout).json()
            except Exception as e:
                if i >= retry:
                    raise PanError(f"网络异常: {e}")
                time.sleep(3)
                continue
            errno = r.get("errno", 0)
            if errno == 31034 and i < retry:
                time.sleep(20)
                continue
            return r
        return r

    # ---------- 列目录 ----------
    def list_dir(self, path: str, recursive: bool = False, limit: int = 1000):
        """列出目录内容；recursive=True 时递归全部子目录。
        返回 [{path, name, size, isdir, mtime}]"""
        root = self.check_path(path)
        out, stack = [], [root]
        while stack:
            d = stack.pop()
            start = 0
            while True:
                r = self._get(LIST_URL, {"dir": d, "start": start,
                                         "limit": limit, "order": "name"})
                if r.get("errno", 0) != 0:
                    raise PanError(f"列目录失败 {d}: errno={r.get('errno')} {r}")
                items = r.get("list", [])
                for it in items:
                    rec = {
                        "path": it.get("path", ""),
                        "name": it.get("server_filename", ""),
                        "size": it.get("size", 0) or 0,
                        "isdir": bool(it.get("isdir")),
                        "mtime": it.get("server_mtime") or it.get("local_mtime") or 0,
                    }
                    out.append(rec)
                    if rec["isdir"] and recursive:
                        stack.append(rec["path"])
                if len(items) < limit:
                    break
                start += limit
            if not recursive:
                break
        return out

    def list_files(self, path: str, recursive: bool = True):
        """只要文件，不要目录"""
        return [f for f in self.list_dir(path, recursive=recursive) if not f["isdir"]]

    # ---------- 建目录 ----------
    def mkdir(self, path: str) -> bool:
        p = self.check_path(path)
        r = self._post(CREATE_URL, {}, data={"path": p, "isdir": 1, "rtype": 0})
        return r.get("errno", 0) in (0, -8, 12)     # -8/12 = 已存在

    # ---------- filemanager 批量操作 ----------
    def _filemanager(self, opera: str, filelist, ondup: str = "skip"):
        """提交一批操作，返回 (成功数, 失败明细列表)"""
        import json as _json
        ok, fails = 0, []
        for i in range(0, len(filelist), BATCH_LIMIT):
            chunk = filelist[i:i + BATCH_LIMIT]
            r = self._post(FILEMANAGER, {"opera": opera, "async": "0"}, data={
                "filelist": _json.dumps(chunk, ensure_ascii=False),
                "ondup": ondup,
            })
            errno = r.get("errno", 0)
            if errno == 0:
                # 同步模式下 info 里是逐条结果，成功 errno=0
                ok += len(chunk)
                for info in r.get("info", []) or []:
                    if isinstance(info, dict) and info.get("errno", 0) != 0:
                        ok -= 1
                        fails.append({"path": info.get("path", "?"),
                                      "errno": info.get("errno"),
                                      "msg": errno_text(info.get("errno"))})
            else:
                for it in chunk:
                    fails.append({"path": it.get("path") if isinstance(it, dict) else it,
                                  "errno": errno, "msg": errno_text(errno)})
        return ok, fails

    def rename_batch(self, ops, ondup="skip"):
        """ops: [{path, newname}]"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            clean.append({"path": p, "newname": o["newname"]})
        return self._filemanager("rename", clean, ondup=ondup)

    def move_batch(self, ops, ondup="skip"):
        """ops: [{path, dest}]  dest 为目标**目录**"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            clean.append({"path": p, "dest": self.check_path(o["dest"])})
        return self._filemanager("move", clean, ondup=ondup)

    def delete_batch(self, paths):
        clean = [{"path": self.check_path(p)} for p in paths]
        return self._filemanager("delete", clean)


def errno_text(errno):
    return {
        0: "成功", -6: "Token 无效", -7: "文件不存在", -8: "文件已存在",
        2: "参数错误", 12: "批量操作部分失败", 111: "Token 过期",
        31034: "命中频控", 31061: "文件不存在", 31066: "文件不存在或无权限",
    }.get(errno, f"错误码 {errno}")


# ===========================================================================
# 改名：四种模式
# ===========================================================================

# 常见下载站/资源站的广告尾巴（clean 模式用）
_AD_PATTERNS = [
    # 1. 方括号/圆括号里带网址的整块广告：[www.xxx.com]、[xxx.com整理]
    r"[\[\(【][^\]\)】]{0,40}(www\.|http|https|\.com|\.net|\.cn|\.org)[^\]\)】]{0,40}[\]\)】]",
    # 2. 裸网址 / 域名（含 http://、www. 前缀，或纯域名）
    r"[\(（]?\s*(?:https?://)?(?:www\.)?[\w\-]+\.(?:com|cn|net|org|io|me|top|xyz)(?:\.[a-z]{2,3})?(?:/[\w\-./?%&=]*)?\s*[\)）]?",
    # 3. 书名号/方括号里的资源站套话：【免费下载】、【xxx首发】
    r"[【\[][^】\]]{0,60}(?:首发|独家|整理|收集|电子书|txt下载|免费下载|全集|完结)[^】\]]{0,60}[】\]]",
    # 4. 网址后面拖着的中文尾巴：——收集整理 / -整理 / —首发
    r"[—–\-_]{1,2}\s*(?:收集整理|整理收集|收集|整理|首发|独家|出品|制作|发布)",
    # 5. 纯长串域名（无分隔符时兜底）
    r"[a-zA-Z0-9]{6,}\.(?:com|cn|net|org)",
]


def split_name(name: str):
    """拆成 (主名, 扩展名)，点开头或无后缀时扩展名为空"""
    if "." not in name[1:]:
        return name, ""
    base, ext = name.rsplit(".", 1)
    if not ext or len(ext) > 8:      # 最后一段太长不当作扩展名
        return name, ""
    return base, ext


def clean_name(name: str) -> str:
    """clean 模式：去广告、去网址、合并多余空格与标点"""
    base, ext = split_name(name)
    s = base
    for pat in _AD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    # 去掉残留的空括号
    s = re.sub(r"[（(\[【]\s*[）)\]】]", "", s)
    # 全角括号包裹的纯英文站点名
    s = re.sub(r"[（(]\s*[A-Za-z0-9\-_.]{3,}\s*[）)]", "", s)
    # 收尾：合并空格、去掉行尾分隔符
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^[\s\-_—–、,，.。]+|[\s\-_—–、,，.。]+$", "", s)
    # 不做 NFKC 归一化：它会把全角括号「（）」改成半角，中文名里很难看。
    # 只把全角数字/字母统一成半角（这类差异不影响观感，反而利于排序）。
    s = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" or "Ａ" <= c <= "Ｚ"
                or "ａ" <= c <= "ｚ" else c for c in s)
    if not s:                        # 全被清掉了就保留原名，避免出现空文件名
        return name
    return f"{s}.{ext}" if ext else s


def rename_one(name: str, mode: str, p: dict, index: int = 0) -> str:
    """按模式算出一个文件的新名字；无变化时返回原名"""
    base, ext = split_name(name)
    new_base = base

    if mode == "replace":
        find, repl = p.get("find", ""), p.get("replace", "")
        if find:
            if p.get("case_sensitive"):
                new_base = base.replace(find, repl)
            else:
                new_base = re.sub(re.escape(find), repl, base, flags=re.I)

    elif mode == "affix":
        prefix, suffix = p.get("prefix", ""), p.get("suffix", "")
        new_base = f"{prefix}{base}{suffix}"

    elif mode == "serial":
        start = int(p.get("start", 1))
        width = int(p.get("width", 3))
        num = str(start + index).zfill(width)
        tpl = p.get("template", "{n}_{name}") or "{n}_{name}"
        new_base = tpl.replace("{n}", num).replace("{name}", base)

    elif mode == "regex":
        pat = p.get("pattern", "")
        repl = p.get("repl", "")
        flags = 0 if p.get("case_sensitive") else re.I
        if pat:
            try:
                new_base = re.sub(pat, repl, base, flags=flags)
            except re.error:
                return name      # 正则写错就原样保留

    elif mode == "clean":
        return clean_name(name)

    else:
        return name

    new_base = new_base.strip()
    if not new_base:
        return name
    new = f"{new_base}.{ext}" if ext else new_base
    return new


def build_rename_plan(files, mode: str, params: dict):
    """files: [{path, name, size}] → 返回 ops（只含真正改名的）"""
    ops, unchanged = [], []
    for i, f in enumerate(files):
        new = rename_one(f["name"], mode, params, i)
        if new and new != f["name"]:
            ops.append({"op": "rename", "path": f["path"], "name": f["name"],
                        "newname": new})
        else:
            unchanged.append(f["path"])
    return ops, unchanged


# ===========================================================================
# 整理：按后缀 / 按大类 / 按日期 归目录
# ===========================================================================

CATEGORY_MAP = {
    "视频": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "rmvb", "rm", "ts", "m2ts",
          "webm", "mpg", "mpeg", "3gp", "vob", "m4v", "iso"},
    "音频": {"mp3", "flac", "wav", "ape", "aac", "ogg", "wma", "m4a", "opus", "dsf"},
    "图片": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg",
          "heic", "raw", "cr2", "nef", "psd", "ai"},
    "文档": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
          "rtf", "odt", "csv", "epub", "mobi", "azw", "azw3", "djvu", "chm"},
    "压缩包": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "cab", "arj"},
    "代码": {"py", "js", "ts", "java", "c", "cpp", "h", "hpp", "go", "rs", "php",
          "rb", "sh", "bat", "json", "xml", "yml", "yaml", "sql", "html", "css"},
    "安装包": {"exe", "msi", "dmg", "apk", "deb", "rpm", "pkg", "appimage"},
    "字幕": {"srt", "ass", "ssa", "sub", "vtt", "idx"},
}


def category_of(ext: str) -> str:
    e = (ext or "").lower()
    for cat, exts in CATEGORY_MAP.items():
        if e in exts:
            return cat
    return "其它"


def build_organize_plan(files, by: str, dest: str, sandbox: str):
    """files: [{path,name,size,mtime}] → 移动计划
    by: ext（按后缀） / category（按大类） / date（按修改年月 YYYY-MM）"""
    dest = dest.rstrip("/")
    ops = []
    for f in files:
        _, ext = split_name(f["name"])
        if by == "ext":
            folder = (ext.lower() or "noext")
        elif by == "category":
            folder = category_of(ext)
        elif by == "date":
            mt = f.get("mtime") or 0
            folder = time.strftime("%Y-%m", time.localtime(mt)) if mt else "未知日期"
        else:
            raise PanError(f"未知的整理方式: {by}")
        ops.append({"op": "move", "path": f["path"], "name": f["name"],
                    "dest": f"{dest}/{folder}"})
    return ops


# ===========================================================================
# 删除：按条件筛选
# ===========================================================================

def filter_files(files, f: dict):
    """筛选条件：ext / name_contains / name_regex / min_mb / max_mb"""
    import fnmatch
    exts = {e.strip().lower().lstrip(".") for e in (f.get("exts") or "").split(",") if e.strip()}
    kw = [k.strip() for k in (f.get("keywords") or "").split(",") if k.strip()]
    pattern = (f.get("regex") or "").strip()
    min_mb = float(f.get("min_mb") or 0)
    max_mb = float(f.get("max_mb") or 0)
    keep = []
    for it in files:
        name = it["name"]
        _, ext = split_name(name)
        if exts and ext.lower() not in exts:
            continue
        if kw and not any(k.lower() in name.lower() for k in kw):
            continue
        if pattern:
            try:
                if not re.search(pattern, name, re.I):
                    continue
            except re.error:
                pass
        mb = (it.get("size") or 0) / 1024 / 1024
        if min_mb and mb < min_mb:
            continue
        if max_mb and mb > max_mb:
            continue
        keep.append(it)
    return keep


def build_delete_plan(files, f: dict):
    hits = filter_files(files, f)
    return [{"op": "delete", "path": h["path"], "name": h["name"],
             "size": h.get("size", 0)} for h in hits]


# ===========================================================================
# 计划校验（粘贴 AI 生成的 JSON 时用）
# ===========================================================================

def validate_plan(plan: dict, sandbox: str):
    """校验外部（AI 生成）计划，返回 (ops, errors)"""
    ops = plan.get("ops") if isinstance(plan, dict) else plan
    if not isinstance(ops, list):
        return [], ["计划格式不对：需要 {\"ops\": [...]} 或直接的数组"]
    good, errs = [], []
    sb = sandbox.rstrip("/")
    for i, o in enumerate(ops, 1):
        if not isinstance(o, dict):
            errs.append(f"第 {i} 条不是对象"); continue
        op = (o.get("op") or "").lower()
        path = (o.get("path") or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        if path != sb and not path.startswith(sb + "/"):
            errs.append(f"第 {i} 条越界（不在沙盒 {sb} 内）：{path}"); continue
        if op == "rename":
            if not o.get("newname"):
                errs.append(f"第 {i} 条 rename 缺 newname"); continue
            good.append({"op": "rename", "path": path, "newname": o["newname"]})
        elif op == "move":
            dest = (o.get("dest") or "").strip()
            if not dest.startswith("/"):
                dest = "/" + dest
            if dest != sb and not dest.startswith(sb + "/"):
                errs.append(f"第 {i} 条目标越界：{dest}"); continue
            good.append({"op": "move", "path": path, "dest": dest.rstrip("/")})
        elif op == "delete":
            good.append({"op": "delete", "path": path})
        else:
            errs.append(f"第 {i} 条未知操作: {op}")
    return good, errs


def apply_plan(pan: PanFiles, ops: list):
    """按类型分组执行计划 → 返回统计"""
    stat = {"rename": 0, "move": 0, "delete": 0, "fails": []}
    ren = [{"path": o["path"], "newname": o["newname"]} for o in ops if o["op"] == "rename"]
    mov = [{"path": o["path"], "dest": o["dest"]} for o in ops if o["op"] == "move"]
    dele = [o["path"] for o in ops if o["op"] == "delete"]

    # 移动前先把目标目录都建好（百度 move 不会自动建）
    for d in sorted({m["dest"] for m in mov}):
        try:
            pan.mkdir(d)
        except Exception as e:
            stat["fails"].append({"path": d, "msg": f"建目录失败 {e}"})

    if ren:
        ok, fails = pan.rename_batch(ren)
        stat["rename"] = ok; stat["fails"] += fails
    if mov:
        ok, fails = pan.move_batch(mov)
        stat["move"] = ok; stat["fails"] += fails
    if dele:
        ok, fails = pan.delete_batch(dele)
        stat["delete"] = ok; stat["fails"] += fails
    return stat
