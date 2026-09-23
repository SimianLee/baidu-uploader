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
  build_rename_plan(files, mode, params, occupied)  → (ops, unchanged, auto)
  build_organize_plan(files, by, dest)
  build_delete_plan(files, filters)
  build_empty_dir_plan(entries, root, skip)   找出空目录（递归为空的）

改名五模式：replace（查找替换）/ affix（加前后缀）/ serial（序号）
          / regex（正则捕获组）/ clean（去广告与括号）/ title（只保留书名）

命名规则（rename_one / title_name）也被 local_rename.py 复用来改本地文件名——
两边必须是同一套规则，否则「先改名再上传」和「上传后在网盘改名」结果会不一样。

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


class ScanCancelled(Exception):
    """扫描被用户主动停止（只读操作，没有对网盘做任何改动）

    dirs/files 记录中断时已经走了多远，面板据此回显「已停止（扫了 N 个目录）」。"""

    def __init__(self, dirs: int = 0, files: int = 0):
        self.dirs = dirs
        self.files = files
        super().__init__(f"已停止扫描（已遍历 {dirs} 个目录 / 找到 {files} 个文件）")


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
    def list_dir(self, path: str, recursive: bool = False, limit: int = 1000,
                 on_progress=None, should_stop=None, failures=None):
        """列出目录内容；recursive=True 时递归全部子目录。
        返回 [{path, name, size, isdir, mtime}]

        on_progress(dirs_done, files_seen, current_dir) 每列完一个目录回调一次。
        目录总数事先未知，只能报「已处理多少」，面板据此显示扫描进度。

        should_stop() 返回真值时立刻抛 ScanCancelled 中断扫描（用户点了「停止」）。
        检查点放在「每层目录开始前」和「同层分页拉取前」——后者保证一个几万条的
        大目录在翻页途中也能被刹住，而不是必须读完这一层。

        failures: 传一个 list 进来即开启**容错模式**。子目录列不开时不再中断整次扫描，
                  而是把 {"path", "errno", "msg"} 追加进去后跳过，继续扫别的。
                  为什么需要：实测百度会返回这种脏条目——父目录的列表里有它
                  （isdir=1），拿它自己的 path 去列却稳定回 -9；试过 trim 尾部空白、
                  全角空格换半角、去掉全角空格等变体全部无效（见 _probe_ls2.py：
                  69 个子目录里就 1 个这样）。几千个目录的大扫描里只要摊上一个，
                  整次预览就全废了，而它跟其余 99.9% 的目录毫无关系。
                  调用方拿到的是同一个 list 对象（可变），不需要接返回值。
                  注意：**起点目录**列不开时无论是否容错都照样抛错——那说明路径或
                  沙盒配置本身有问题，必须让用户当场看见，而不是给个空结果。
        """
        root = self.check_path(path)
        out, stack, dirs_done, files_seen = [], [root], 0, 0
        while stack:
            d = stack.pop()
            start = 0
            broken = False
            while True:
                if should_stop and should_stop():
                    raise ScanCancelled(dirs_done, files_seen)
                r = self._get(LIST_URL, {"dir": d, "start": start,
                                         "limit": limit, "order": "name"})
                e = r.get("errno", 0)
                if e != 0:
                    # 这几种是全局性的（授权坏了 / 被限流），跳过没有任何意义：
                    # 后面每个目录都会同样失败，此时静默跳过只会给出一个假结果
                    if (failures is not None and d != root
                            and e not in _FATAL_LIST_ERRNOS):
                        if len(failures) < 500:     # 兜个上限，别让响应体爆炸
                            failures.append({"path": d, "errno": e,
                                             "msg": errno_text(e)})
                        broken = True
                        break
                    raise PanError(list_dir_error(d, e))
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
                    if rec["isdir"]:
                        if recursive:
                            stack.append(rec["path"])
                    else:
                        files_seen += 1
                if len(items) < limit:
                    break
                start += limit
            dirs_done += 1
            if on_progress:
                try:
                    on_progress(dirs_done, files_seen, d)
                except Exception:
                    pass        # 进度回调只是锦上添花，出错绝不能中断扫描
            if broken:
                continue        # 这个目录列不开，已记进 failures；接着扫栈里剩下的
            if not recursive:
                break
        if should_stop and should_stop():       # 收尾前最后一道检查
            raise ScanCancelled(dirs_done, files_seen)
        return out

    def list_files(self, path: str, recursive: bool = True, on_progress=None,
                   should_stop=None, failures=None):
        """只要文件，不要目录（failures 的语义见 list_dir）"""
        return [f for f in self.list_dir(path, recursive=recursive,
                                         on_progress=on_progress,
                                         should_stop=should_stop,
                                         failures=failures)
                if not f["isdir"]]

    # ---------- 建目录 ----------
    def mkdir(self, path: str) -> bool:
        p = self.check_path(path)
        r = self._post(CREATE_URL, {}, data={"path": p, "isdir": 1, "rtype": 0})
        return r.get("errno", 0) in (0, -8, 12)     # -8/12 = 已存在

    # ---------- filemanager 批量操作 ----------
    def _filemanager(self, opera: str, filelist, ondup: str = "skip",
                     on_progress=None):
        """提交一批操作，返回 (成功数, 失败明细列表)

        filelist 超过 BATCH_LIMIT 会自动拆成多批提交，每批发一次请求。
        每批处理完调一次 on_progress(本批条数, 本批失败条数)：
        一次几百条要等好几分钟，没有进度反馈会让人以为卡死了。
        失败条数必须由这里给出——调用方要等本方法返回后才拿得到 fails，
        等它统计的话进度里的失败数会滞后一批。
        """
        import json as _json
        ok, fails = 0, []
        for i in range(0, len(filelist), BATCH_LIMIT):
            chunk = filelist[i:i + BATCH_LIMIT]
            fails_before = len(fails)
            r = self._post(FILEMANAGER, {"opera": opera, "async": "0"}, data={
                "filelist": _json.dumps(chunk, ensure_ascii=False),
                "ondup": ondup,
            })
            errno = r.get("errno", 0)
            info = [x for x in (r.get("info") or []) if isinstance(x, dict)]
            if errno == 0 or info:
                # 同步模式下 info 里是逐条结果。errno=0 表示这批全成功；
                # errno=12 是「部分失败」，逐条结果同样在 info 里——必须按条统计，
                # 否则成功的也会被算成失败，真正的原因码（-8 撞名 / -9 不存在）也丢了。
                judged = set()
                for it_ in info:
                    judged.add(it_.get("path"))
                    if it_.get("errno", 0) != 0:
                        fails.append({"path": it_.get("path", "?"),
                                      "errno": it_.get("errno"),
                                      "msg": errno_text(it_.get("errno"))})
                if errno != 0:      # info 没覆盖到的条目，结论只能看外层 errno
                    for it in chunk:
                        if it.get("path") not in judged:
                            fails.append({"path": it.get("path"), "errno": errno,
                                          "msg": errno_text(errno)})
                ok += len(chunk) - (len(fails) - fails_before)
            else:
                for it in chunk:
                    fails.append({"path": it.get("path") if isinstance(it, dict) else it,
                                  "errno": errno, "msg": errno_text(errno)})
            if on_progress:
                try:
                    on_progress(len(chunk), len(fails) - fails_before)
                except Exception:
                    pass        # 进度回调只是锦上添花，出错绝不能中断执行
        return ok, fails

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, newname}]"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            clean.append({"path": p, "newname": o["newname"]})
        return self._filemanager("rename", clean, ondup=ondup,
                                 on_progress=on_progress)

    def move_batch(self, ops, ondup="skip", on_progress=None):
        """ops: [{path, dest}]  dest 为目标**目录**"""
        clean = []
        for o in ops:
            p = self.check_path(o["path"])
            clean.append({"path": p, "dest": self.check_path(o["dest"])})
        return self._filemanager("move", clean, ondup=ondup,
                                 on_progress=on_progress)

    def delete_batch(self, paths, on_progress=None):
        clean = [{"path": self.check_path(p)} for p in paths]
        return self._filemanager("delete", clean, on_progress=on_progress)


def errno_text(errno):
    # 逐条明细里只报简短原因；「百度常假报」这类解释统一放在执行结果提示里，
    # 每条明细都带一遍会啰嗦到看不清
    return {
        0: "成功", -6: "Token 无效", -7: "文件或目录名非法", -8: "文件已存在",
        -9: "文件不存在", -10: "云盘容量不足", -11: "文件超长", -12: "文件名非法",
        2: "参数错误", 12: "批量操作部分失败", 111: "Token 过期",
        31034: "命中频控", 31061: "文件不存在", 31066: "文件不存在或无权限",
    }.get(errno, f"错误码 {errno}")


# 列目录时「跳过没意义、必须当场报错」的几种错误：授权类是全局性的（后面每个
# 目录都会同样失败），31034 是限流（_get 里已经退避重试过，还失败说明真被限住了，
# 继续扫下去只会满屏失败、给出一个假的「扫完了」）
_FATAL_LIST_ERRNOS = {-6, 111, 31034}


def list_dir_error(path: str, errno) -> str:
    """把「列目录失败」翻成人话。

    之前是直接把原始响应 JSON 甩出去（errno=-9 {'errno': -9, 'request_id': ...}），
    用户看到一串方块字码点，既不知道出了什么事，也不知道该怎么办。
    """
    why = {
        -6: "授权已失效，请先重新授权",
        111: "授权已过期，请先重新授权",
        31034: "请求太频繁，被百度限流了，歇几分钟再试",
        -9: "目录不存在，可能已被移动或改名",
        31061: "目录不存在，可能已被移动或改名",
        31066: "目录不存在，或没有访问权限",
        -7: "目录名不合法",
    }.get(errno, errno_text(errno))
    return f"列目录失败（{why}）：{path}"


# ===========================================================================
# 改名：五种模式
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


# ---------------------------------------------------------------------------
# title 模式：「只保留书名」（整理小说名用）
#
# 规则是**按信号强度级联**的，命中即停，都没命中就原样返回——宁可不动，也别乱改。
# 依据是本地 6 万个小说文件名的实际形态统计：
#   完整书名号《…》6.4% | 残缺右书名号 …》1.0% | 作者标记 3.6%
#   前导方括号标签 1.7% | 空格-空格 作者 0.5% | 其余本来就不脏
# ---------------------------------------------------------------------------

# 括号里只要出现这些词，**整组**都是标注。
# 必须整组丢：只删关键词会把「(全+二番外)」掏成「(全+二)」，比不删更难看。
_TITLE_TAIL_KW = re.compile(
    r"[（(\[【][^（()）\[\]【】]{0,32}"
    r"(?:全本|全集|完结|番外|出书版|网络版|校对版|精校版|完整版|无删减|修订版|"
    r"TXT|txt|电子书|上部|下部|第[一二三四五六七八九十百\d]{1,3}[部卷册篇]|"
    r"[\d一二三四五六七八九十百]{1,3}[部卷册集篇])"
    r"[^（()）\[\]【】]{0,32}[）)\]】]")

# 括号里就一个单字卷次标记：【全】、（上）、（下）
# 单独一支而不是并进上面：并进去的话「（李上校）」这种会被「含上字」误伤
_TITLE_TAIL_SINGLE = re.compile(r"[（(\[【]\s*[上下全]\s*[）)\]】]")

# 括号里只有一个「卷次 / 册数 / 区间」：(1-7)、（3）、(12)、（第5卷）
#
# 单个数字那一支必须 (?<!\s)——**前面有空格就不动**。因为撞名自动编号生成的就是
# 「同名 (2)」，若把它也当卷次清掉，第二次扫描会再把「同名 (2)」改回「同名」，
# 名字被反复搅动、永不收敛（实测踩过：改名后重扫仍有 2 条待改）。
_TITLE_TAIL_VOL = re.compile(
    r"(?<!\s)[（(\[【]\s*(?:第)?[\d一二三四五六七八九十百千]{1,4}"
    r"(?:[\-~～—–至,，、][\d一二三四五六七八九十百千]{1,4})*"
    r"\s*(?:[部卷册集篇])?\s*[）)\]】]")

# 收尾的裸标注：全TXT格式电子书_003 / 全本 / 完结 / +番外
_TITLE_TAIL_BARE = re.compile(
    r"(?:全?\s*TXT\s*格式\s*电子书|全本\s*TXT|TXT\s*格式|电子书|"
    r"\+\s*番外|番外|完结|全本|全集)(?:[_\-\s]*\d{1,4})?\s*$", re.I)

# 作者标记。by 必须卡边界，否则 "Baby"、"Hobby" 会被啃掉半截。
# 而且 by 后面要求是「：」或中日韩字——不然英文书名里的 "Stand by Me" 会被切成
# "Stand"。中文上传者写的 by 后面几乎都是中文名，这个限制代价很小。
_RE_AUTHOR = re.compile(
    r"[\s\-_.·]*\s*(?:作者|著者)\s*[：:]?\s*"
    r"|[\s\-_.·]*\s*(?<![A-Za-z])(?:BY|By|by)\s*[：:]\s*"
    r"|[\s\-_.·]*\s*(?<![A-Za-z])(?:BY|By|by)\s*(?=[\u4e00-\u9fff])")

_RE_LEAD_TAG = re.compile(r"^\s*[\(\[【][^\)\]】]{0,24}[\)\]】]\s*")
# 「空格-横杠-空格」是作者分隔符的常见写法：边荒传说 - 黄易 / Rework - Jason Fried
# 要求两侧都有空格：不然「半-城」这种书名里的连字符会被误当分隔符
_RE_SEP = re.compile(r"\s+[-—–]\s+")
# 前导序号：16、书名 → 书名。分隔符必须显式存在，否则「24个比利」会被啃成「个比利」
_RE_LEAD_NUM = re.compile(r"^\s*\d{1,4}\s*[、.．,，]\s*(?=[^\d\s])")
_RE_LEAD_JUNK = re.compile(r"^[\s\-_—–、,，.。·@]+|[\s\-_—–、,，.。·@]+$")
# 「整个名字就是一对括号」的情况。内容里必须不含任何括号字符——否则
# 首尾各是一个括号的 [棋魂]因为爱你 作者：一叶（亮光） 会被错拆成
# 「棋魂]因为爱你 作者：一叶（亮光」（内容可以吞掉中间的 ]，fullmatch 照样成立）
_RE_WHOLE_WRAP = re.compile(r"[（(\[【]\s*([^（()）\[\]【】]{1,60}?)\s*[）)\]】]")


def title_name(name: str) -> str:
    """title 模式：把小说文件名清成「只有书名」，无变化时返回原名

    级联顺序（先强信号，后弱信号）：
      1. 完整书名号《…》→ 取括号内。天然跳过《《一光年之恋》》这种错套：
         因为要求内容里不含书名号，第一个能配上的其实是内层那一对。
      2. 残缺书名号 → 取最靠左的那个符号，右残取它前面、左残取它后面。
         真实数据里这种残缺名很多：暗香情撩》by：季安 / 单飞雪《甜上眉梢
      3. 作者标记（作者：/著者/BY：）→ 取标记之前的部分
      4. 前导标签 [悬疑] / 【类型】 → 去掉
      5. 「空格-空格」作者分隔 → 取前面：边荒传说 - 黄易 / Rework - Jason Fried
    最后统一清尾部标注（全本/完结/番外/TXT/卷次，整组丢）。
    清成空名字时返回原名——宁可保留脏名字，也不能产生无名文件。
    """
    base, ext = split_name(name)
    s = base.strip()
    if not s:
        return name

    # 先把广告尾巴去掉，它们会把书名号切碎（[xxx.com]《书名》这种）
    for pat in _AD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I)
    s = s.strip()
    if not s:
        return name

    m = re.search(r"《([^《》]{1,80})》", s)
    if m:
        s = m.group(1)
    else:
        # 残缺书名号：谁在前面谁说了算
        pos_close, pos_open = s.find("》"), s.find("《")
        if pos_close > 0 and (pos_open < 0 or pos_close < pos_open):
            s = s[:pos_close]
        elif pos_open >= 0 and pos_open + 1 < len(s):
            s = s[pos_open + 1:]
        else:
            # 整个名字就是一对括号包着的：【美人】→ 美人
            mw = _RE_WHOLE_WRAP.fullmatch(s)
            if mw and mw.group(1).strip():
                s = mw.group(1)
            else:
                ma = _RE_AUTHOR.search(s)
                if ma:
                    s = s[:ma.start()]
                else:
                    s2 = _RE_LEAD_TAG.sub("", s)
                    if s2 != s:
                        s = s2
                    else:
                        ms = _RE_SEP.search(s)
                        if ms:
                            s = s[:ms.start()]

    # 尾部标注清理：反复擦到不动为止（「（全本）+番外」要两轮才干净）
    for _ in range(4):
        prev = s
        s = _TITLE_TAIL_KW.sub("", s)
        s = _TITLE_TAIL_SINGLE.sub("", s)
        s = _TITLE_TAIL_VOL.sub("", s)
        s = _TITLE_TAIL_BARE.sub("", s)
        s = _RE_LEAD_TAG.sub("", s)
        s = _RE_LEAD_NUM.sub("", s)
        s = s.replace("《", "").replace("》", "")
        s = _RE_LEAD_JUNK.sub("", s)
        s = re.sub(r"\s{2,}", " ", s).strip()
        if s == prev or not s:
            break

    if not s:
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

    elif mode == "title":
        return title_name(name)

    else:
        return name

    new_base = new_base.strip()
    if not new_base:
        return name
    new = f"{new_base}.{ext}" if ext else new_base
    return new


def dedupe_newnames(rows, occupied=None):
    """新名字撞车时自动加 (2)(3)…

    rows: [{"dir": 所在目录, "name": 原名, "newname": 目标名}]，**原地改写** newname
    occupied: {目录: {已占用的小写名字}}。本地改名时用磁盘实况填（同目录里不会
              改名的那些文件）；网盘侧事先不知道目录内容，只能做「本批内部」去重，
              撞到网盘上已有的同名文件由 ondup 策略（跳过/覆盖）兜底。

    为什么必须去重：只保留书名会把「《A》作者：甲.txt」「A - 乙.txt」压成同一个
    「A.txt」。执行时两条争一个名字，必然一条成功一条报错，还会留下说不清的半截
    状态。加个 (2) 就都保住了，信息量也比丢掉一条强。

    返回被自动编号的条数（要如实告诉用户，不能悄悄改名）。
    """
    used = {}
    if occupied:
        for d, names in occupied.items():
            used[d] = {n.lower() for n in names}
    auto = 0
    for r in rows:
        pool = used.setdefault(r["dir"], set())
        want = r["newname"]
        if want.lower() in pool:
            base, ext = split_name(want)
            k = 2
            while True:
                cand = f"{base} ({k})" + (f".{ext}" if ext else "")
                if cand.lower() not in pool:
                    break
                k += 1
            r["newname"] = cand
            auto += 1
        pool.add(r["newname"].lower())
    return auto


def build_rename_plan(files, mode: str, params: dict, occupied=None):
    """files: [{path, name, size}] → 返回 (ops, unchanged, auto)

    ops 只含真正改名的；auto 是撞名被自动编号的条数。
    """
    ops, unchanged = [], []
    for i, f in enumerate(files):
        new = rename_one(f["name"], mode, params, i)
        if new and new != f["name"]:
            ops.append({"op": "rename", "path": f["path"], "name": f["name"],
                        "newname": new})
        else:
            unchanged.append(f["path"])
    rows = [{"dir": o["path"].rpartition("/")[0], "name": o["name"],
             "newname": o["newname"]} for o in ops]
    before = [r["newname"] for r in rows]
    auto = dedupe_newnames(rows, occupied) if rows else 0
    for o, r, b in zip(ops, rows, before):
        o["newname"] = r["newname"]
        if r["newname"] != b:
            o["auto"] = True        # 撞名被自动编号，预览里要标出来
    return ops, unchanged, auto


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
    e = (ext or "").lower().lstrip(".")   # 传 ".mp4" 或 "mp4" 都能匹配
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
# 空目录：找出「整棵子树里连一个文件都没有」的目录
# ===========================================================================

def build_empty_dir_plan(entries, root: str, skip: str = "", unreadable=None):
    """找出能删掉的空目录 → 返回 (ops, info)

    entries: PanFiles.list_dir(root, recursive=True) 的**原始**结果——目录和文件都要。
             不能用 list_files()，它把目录滤掉了，而这里要判定对象正是目录。
    root:    扫描起点，**它自己不参与删除**。两个理由：用户正站在这个目录里，
             把它删掉太意外；而且它下面若全是空的，删掉它的直接子目录同样清得干净。
    skip:    逗号分隔的名字关键字，命中的目录**连同整棵子树**一并跳过
             （默认由调用方传 "_覆盖备份" —— 别顺手把备份区连根端了）。
    unreadable: 扫描时**列不开**的目录路径列表（list_dir 的 failures）。
             这些目录里有没有文件是无从得知的，必须当成「非空」保护起来，
             否则「不知道有什么」会被当成「什么都没有」而删掉——那是用户的真数据。
             （实测百度会返回这种东西：父目录里有它，拿它的 path 去列回 -9。）

    判定规则：某目录（含所有层级的子目录）里一个文件都没有 ⇒ 空。
    被 skip 命中的目录自己不算，**它的祖先也一律不算**——否则删掉那个祖先会把
    受保护的目录一起捎走（典型场景：「只装着一个空的 _覆盖备份」的父目录）。
    返回的 ops 只含「最外层」空目录——删掉它，嵌在里面的空目录跟着消失。
    不逐层列出来的原因：一是预览会啰嗦几倍，二是后几条必然报 -9「文件不存在」
    的假失败（父目录一删，子目录就没了），把真实的失败也淹没掉。

    info = {empty_total 空目录总数, nested 会随根目录一起消失的嵌套数,
            dirs_scanned 扫到的目录数, files_scanned 扫到的文件数}
    """
    root = (root or "").rstrip("/")
    kws = [k.strip().lower() for k in (skip or "").split(",") if k.strip()]

    dirs, files_scanned = [], 0
    for e in entries:
        p = (e.get("path") or "").rstrip("/")
        if not p:
            continue
        if e.get("isdir"):
            if p != root:           # 兜底：list_dir 本就不会返回起点自己
                dirs.append(p)
        else:
            files_scanned += 1

    def rel_segs(p):
        rel = p[len(root):] if p.startswith(root) else p
        return [s.lower() for s in rel.strip("/").split("/") if s]

    def is_skipped(p):
        """自己或任意一级祖先命中关键字 ⇒ 跳过（整棵子树都不许动）"""
        return any(k in seg for seg in rel_segs(p) for k in kws)

    # 1. 目录里有没有文件：先标记文件的直属目录，再自下而上传染给祖先。
    #    被跳过的目录也要照常参与判定——否则「只含一个被跳过的空目录」的父目录
    #    会被误判成空，进而把受保护的目录一起带走。
    has_file = {d: False for d in dirs}
    for e in entries:
        if e.get("isdir"):
            continue
        parent = (e.get("path") or "").rstrip("/").rsplit("/", 1)[0]
        if parent in has_file:      # 直属 root 的文件：root 不入表，无需标记
            has_file[parent] = True
    # 2. 子树里有没有被跳过的目录，同样自下而上传染：父目录即便自己一个文件都没有，
    #    只要子树里藏着受保护的目录，它就不能算「可删的空目录」——删了会把保护对象带走
    skip_sub = {d: is_skipped(d) for d in dirs}
    for d in sorted(dirs, key=lambda x: x.count("/"), reverse=True):
        parent = d.rsplit("/", 1)[0]
        if parent not in has_file:
            continue
        if has_file[d]:
            has_file[parent] = True
        if skip_sub[d]:
            skip_sub[parent] = True

    # 2b. 打不开的目录（list_dir 的 failures）：里面装了什么我们**根本不知道**，
    #     绝不能当空目录删掉——就是那个「肖申克的救赎 斯蒂芬•金　」，天知道底下
    #     有没有书。它的祖先同样不能算空：删掉祖先会把它一起带走。所以把「它自己
    #     加上到 root 为止的每一级祖先」整条链都打上保护（精确路径匹配，不走 skip
    #     那套关键字——关键字是给人配的，这个是扫描的客观结果）
    prot = set()
    for u in (unreadable or []):
        p = (u or "").rstrip("/")
        while p and p != root and p.startswith(root + "/"):
            prot.add(p)
            p = p.rsplit("/", 1)[0]

    # 3. 空的里面，剔掉被 skip 命中的子树（自己命中，或子树里藏着命中的），
    #    以及所有打不开、无法确认是否真的空的目录
    cand = [d for d in dirs if not has_file[d] and not skip_sub[d] and d not in prot]

    # 4. 只留最外层：父目录也是候选的话，自己会随父目录一起消失，不必单列
    cand_set = set(cand)
    roots = [d for d in cand if d.rsplit("/", 1)[0] not in cand_set]
    roots.sort(key=lambda x: -x.count("/"))    # 深层在前，稳妥

    ops = [{"op": "delete", "path": d, "name": d.rsplit("/", 1)[-1], "isdir": True}
           for d in roots]
    info = {"empty_total": len(cand), "nested": len(cand) - len(roots),
            "dirs_scanned": len(dirs), "files_scanned": files_scanned,
            "protected": len(prot)}
    return ops, info


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
            # isdir 要带下去：删空文件夹与删文件在提示文案上必须分开说，
            # 丢掉这个标记就只能把「删除 12 个空文件夹」写成「删除 12 个文件」
            good.append({"op": "delete", "path": path,
                         **({"isdir": True} if o.get("isdir") else {})})
        else:
            errs.append(f"第 {i} 条未知操作: {op}")
    return good, errs


def _find_conflicts(pan: PanFiles, ren, mov):
    """找出将被 rename/move 撞到的网盘现有文件，返回它们的完整路径列表。

    rename 撞名点 = 源文件所在目录 + newname；move 撞名点 = dest + 源文件名。
    每个涉及目录只 list 一次（缓存），文件名按小写比较（网盘大小写不敏感）。
    """
    targets = {}                       # 目录 -> {小写目标名}
    for o in ren:
        d, _, n = o["path"].rpartition("/")
        targets.setdefault(d, set()).add(o["newname"].lower())
    for o in mov:
        targets.setdefault(o["dest"], set()).add(o["path"].rsplit("/", 1)[-1].lower())

    conflicts = []
    for d, names in targets.items():
        try:
            entries = pan.list_dir(d)
        except Exception:
            continue                   # 目录不存在=必然不撞名，跳过
        for e in entries:
            if e.get("isdir"):
                continue
            if e.get("name", "").lower() in names:
                conflicts.append(f"{d}/{e['name']}")
    return conflicts


def _mkdirs(pan: PanFiles, path: str):
    """一次建多级目录（百度 create 支持多级路径；已存在返回 True）"""
    try:
        return pan.mkdir(path)
    except Exception:
        return False


def apply_plan(pan: PanFiles, ops: list, ondup: str = "skip", on_progress=None):
    """按类型分组执行计划 → 返回统计

    ondup: rename/move 撞到同名文件时的策略
           skip      跳过该条，保留网盘上已有的文件（默认，安全）
           overwrite 覆盖。注意：百度 filemanager 的 ondup 参数对 rename/move
                     不生效（实测撞名一律报 -8），所以覆盖用「先挪备份再执行」
                     实现：被撞的旧文件移动到 沙盒/_覆盖备份/时间戳/原目录结构
                     下，不删除、可找回，之后原操作就不会撞名了。

    on_progress(snap): 每完成一批回调一次，用来显示执行进度。snap =
        {"done": 已完成动作数, "total": 总动作数, "phase": 当前阶段文案,
         "fails": 失败条数}
        总动作数 = 改名 + 移动 + 删除，覆盖模式下再加「备份被撞文件」的条数。
        一次几百条要跑好几分钟，没有这个回调前端只能干等。
    """
    stat = {"rename": 0, "move": 0, "delete": 0, "backup": 0,
            "backup_dir": "", "fails": []}
    ren = [{"path": o["path"], "newname": o["newname"]} for o in ops if o["op"] == "rename"]
    mov = [{"path": o["path"], "dest": o["dest"]} for o in ops if o["op"] == "move"]
    dele = [o["path"] for o in ops if o["op"] == "delete"]

    # 进度快照。用可变 dict 而非局部变量：闭包里改它不需要 nonlocal
    prog = {"done": 0, "total": len(ren) + len(mov) + len(dele),
            "phase": "", "fails": 0}

    def emit(phase=None):
        if phase:
            prog["phase"] = phase
        if on_progress:
            try:
                on_progress(dict(prog))
            except Exception:
                pass        # 进度显示出问题，绝不能影响真正的执行

    def tick(phase):
        """生成「一批提交完成」的回调：累加进度与失败数，然后汇报

        失败数由底层连同本批条数一起给出。不能等本函数返回后再从 stat 里数，
        那样进度上的失败数会滞后一批。
        """
        def cb(k, nf=0):
            prog["done"] += k
            prog["fails"] += nf
            emit(phase)
        return cb

    # 第一批返回前可能等好几分钟（百度按批处理），文案得让人知道是在等网盘，
    # 而不是「准备中」那种看不出进展的说法
    emit("提交中")

    # 覆盖模式：先找出撞名文件，挪进带时间戳的备份目录（保留原路径结构）
    if ondup == "overwrite" and (ren or mov):
        emit("检查重名文件")
        conflicts = _find_conflicts(pan, ren, mov)
        if conflicts:
            bdir = f"{pan.sandbox}/_覆盖备份/{time.strftime('%Y%m%d-%H%M%S')}"
            mv_ops = []
            for cf in conflicts:
                rel = cf[len(pan.sandbox) + 1:]
                bdest = f"{bdir}/{str(PurePosixPath(rel).parent)}"
                _mkdirs(pan, bdest)
                mv_ops.append({"path": cf, "dest": bdest})
            # 备份也是要实现的动作，算进总数；否则进度条先跑到别处再卡住，看着像死机
            prog["total"] += len(mv_ops)
            ok, fails = pan.move_batch(mv_ops, on_progress=tick("备份被撞的旧文件"))
            stat["backup"] = ok
            stat["backup_dir"] = bdir
            stat["fails"] += fails
            if fails:
                # 有文件没备份成功就不能继续覆盖（会导致误删），直接返回
                stat["fails"].insert(0, {"path": bdir,
                                         "msg": "备份失败，覆盖中止（见下方明细）"})
                return stat
            time.sleep(0.5)            # 等索引同步，避免紧跟着的操作读到旧状态

    # 移动前先把目标目录都建好（百度 move 不会自动建）
    dirs = sorted({m["dest"] for m in mov})
    for i, d in enumerate(dirs, 1):
        try:
            pan.mkdir(d)
        except Exception as e:
            stat["fails"].append({"path": d, "msg": f"建目录失败 {e}"})
            prog["fails"] += 1
        emit(f"准备目标目录 {i}/{len(dirs)}")     # 目录多时也得让人看到在动

    if ren:
        ok, fails = pan.rename_batch(ren, ondup="skip", on_progress=tick("正在改名"))
        stat["rename"] = ok; stat["fails"] += fails
    if mov:
        ok, fails = pan.move_batch(mov, ondup="skip", on_progress=tick("正在移动"))
        stat["move"] = ok; stat["fails"] += fails
    if dele:
        ok, fails = pan.delete_batch(dele, on_progress=tick("正在删除"))
        stat["delete"] = ok; stat["fails"] += fails
    emit("已完成")
    return stat
