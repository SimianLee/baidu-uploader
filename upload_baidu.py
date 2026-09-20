#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘分批上传工具（非会员友好版）

功能：
  1. 把本地指定目录的文件分批上传到百度网盘指定目录（非会员有上传数量限制，分批 + 批间暂停）
  2. 超过指定大小的文件自动跳过并单独列出（防止大文件上传失败浪费时间）
  3. 上传成功的文件，可手动确认后：移动到本地"已完成"目录，或送入回收站
  4. 支持断点续传（重跑时自动跳过之前已成功的文件）、秒传、干跑预览

授权方式：百度网盘开放平台「设备码」扫码授权，Token 自动保存与刷新。

用法示例：
  python upload_baidu.py --login                 # 首次登录授权（扫码）
  python upload_baidu.py --dry-run               # 干跑：只列出分批计划，不真上传
  python upload_baidu.py                         # 按 config.json 正常上传
  python upload_baidu.py --batch-size 30         # 临时覆盖每批数量
"""

import argparse
import atexit
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import requests

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

# Windows 控制台强制 UTF-8，避免中文乱码
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------- 常量 ----------------
DEFAULT_CHUNK_MB = 4          # 百度分片上传标准分片 4MB
TOKEN_FILE = "token.json"     # 与脚本放一起
DONE_LOG = "uploaded_log.json"  # 断点记录：已成功上传的本地文件清单
PID_FILE = "upload.pid"       # 上传进程锁：网页重启后也能认出"还有一个上传在跑"

# 常见错误码（够用为主，其余原样打印）
ERRNO_MAP = {
    0: "成功",
    -6: "身份验证失败（Token 无效，请重新 --login）",
    111: "access_token 过期，正在尝试自动刷新",
    2: "参数错误",
    12: "部分文件已经存在于网盘（已按 rtype 策略处理）",
    -8: "文件/目录已存在",
    31034: "命中接口频控，请加大批间暂停时间",
    31061: "文件已经存在网盘中",
    31062: "文件名非法",
    31064: "文件被和谐/违规",
    -7: "文件或目录名错误或无权访问",
}


# ---------------- 工具函数 ----------------
def load_json(path: Path, default):
    if path.exists():
        try:
            # utf-8-sig：兼容 Windows 记事本保存的带 BOM 文件
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.2f} TB"


def errno_msg(errno) -> str:
    if errno is None:
        return "未知错误（无 errno）"
    return f"errno={errno} {ERRNO_MAP.get(errno, '未收录的错误码，请到百度开放平台文档查询')}"


def pid_alive(pid: int) -> bool:
    """跨进程判断 PID 是否还活着（Windows 用 OpenProcess，避开 os.kill 的语义差异）"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_pidfile(path: Path):
    """占住进程锁：已有活着的上传进程就返回 (False, 那个PID)，否则写入自己的 PID 并注册退出清理"""
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip())
        except Exception:
            old = -1
        if pid_alive(old):
            return False, old
    path.write_text(str(os.getpid()), encoding="utf-8")
    me = os.getpid()

    def _cleanup():
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(me):
                path.unlink()
        except Exception:
            pass
    atexit.register(_cleanup)
    return True, me


# ---------------- 授权（设备码扫码） ----------------
class BaiduAuth:
    """设备码授权：去 https://openapi.baidu.com/device 输入 user_code 扫码确认"""

    DEVICE_CODE_URL = "https://openapi.baidu.com/oauth/2.0/device/code"
    TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"

    def __init__(self, app_key: str, secret_key: str, token_path: Path):
        self.app_key = app_key
        self.secret_key = secret_key
        self.token_path = token_path
        self.tokens = load_json(token_path, {})

    @property
    def access_token(self):
        return self.tokens.get("access_token", "")

    def login(self):
        r = requests.get(self.DEVICE_CODE_URL, params={
            "response_type": "device_code",
            "client_id": self.app_key,
            "scope": "basic,netdisk",
        }, timeout=15).json()
        if "error" in r:
            print(f"[错误] 获取设备码失败：{r}")
            print("请检查 config.json 里的 app_key 是否正确（需在百度网盘开放平台创建应用）")
            sys.exit(1)

        print("=" * 56)
        print("请打开下面网址，输入验证码并扫码授权（5 分钟内有效）：")
        print(f"  网址: {r.get('verification_url', 'https://openapi.baidu.com/device')}")
        print(f"  验证码: {r['user_code']}")
        print("=" * 56)

        interval = r.get("interval", 5)
        deadline = time.time() + r.get("expires_in", 300)
        while time.time() < deadline:
            time.sleep(interval)
            tr = requests.post(self.TOKEN_URL, params={
                "grant_type": "device_token",
                "code": r["device_code"],
                "client_id": self.app_key,
                "client_secret": self.secret_key,
            }, timeout=15).json()
            if "access_token" in tr:
                self._save_tokens(tr)
                return
            if tr.get("error") not in ("authorization_pending", "expired_token"):
                print(f"[错误] 授权失败：{tr}")
                print("提示：可改用授权码模式 --login-code（不限时、更稳）")
                sys.exit(1)
        print("[错误] 授权超时（5 分钟内未完成扫码），建议改用：--login-code")
        sys.exit(1)

    def login_by_code(self, code: str = ""):
        """授权码模式（备用通道）：浏览器打开网址授权，复制页面显示的授权码换 Token。
        优点：不用跟设备码的 5 分钟轮询赛跑。"""
        url = ("https://openapi.baidu.com/oauth/2.0/authorize?"
               f"response_type=code&client_id={self.app_key}"
               "&redirect_uri=oob&scope=basic,netdisk")
        if not code:
            print("=" * 56)
            print("请用浏览器打开下面网址，登录百度账号并确认授权，")
            print("然后把页面显示的【授权码】输入到下方：")
            print(f"  {url}")
            print("=" * 56)
            code = input("授权码: ").strip()
        if not code:
            print("[错误] 授权码为空")
            sys.exit(1)
        tr = requests.post(self.TOKEN_URL, params={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.app_key,
            "client_secret": self.secret_key,
            "redirect_uri": "oob",
        }, timeout=15).json()
        if "access_token" in tr:
            self._save_tokens(tr)
        else:
            print(f"[错误] 授权码换取 Token 失败：{tr}")
            print("常见原因：授权码只能用一次、已过期（约10分钟）、或复制不完整")
            sys.exit(1)

    def _save_tokens(self, tr: dict):
        self.tokens = tr
        save_json(self.token_path, tr)
        print(f"[OK] 授权成功，access_token 有效期 {tr.get('expires_in', 0)//86400} 天，"
              f"已保存到 {self.token_path.name}（refresh_token 可自动续期）")

    def refresh(self) -> bool:
        rt = self.tokens.get("refresh_token")
        if not rt:
            return False
        tr = requests.post(self.TOKEN_URL, params={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": self.app_key,
            "client_secret": self.secret_key,
        }, timeout=15).json()
        if "access_token" in tr:
            self.tokens = tr
            save_json(self.token_path, tr)
            print("[OK] Token 已自动刷新")
            return True
        return False

    def ensure_token(self) -> str:
        if not self.access_token:
            print("[提示] 本地没有 Token，请先执行: python upload_baidu.py --login")
            sys.exit(1)
        return self.access_token


# ---------------- 网盘 API ----------------
class PanApi:
    PRECREATE = "https://pan.baidu.com/rest/2.0/xpan/file?method=precreate"
    CREATE = "https://pan.baidu.com/rest/2.0/xpan/file?method=create"
    SUPERFILE = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"

    def __init__(self, auth: BaiduAuth):
        self.auth = auth
        self._dirs_created = set()   # 已确认存在的远程目录缓存（海量小文件场景省掉重复 mkdir）

    def _get(self, url, params, retry=2):
        """带自动刷新 Token、频控退避的 GET"""
        params = {**params, "access_token": self.auth.access_token}
        for i in range(retry + 1):
            r = requests.get(url, params=params, timeout=30).json()
            errno = r.get("errno", 0)
            if errno == 111 and self.auth.refresh():   # 过期则刷新重试
                params["access_token"] = self.auth.access_token
                continue
            if errno == 31034 and i < retry:           # 命中频控：退避后重试
                wait = 30 * (i + 1)
                print(f"    [频控] 接口限流，退避 {wait}s 后重试...")
                time.sleep(wait)
                continue
            return r
        return r

    def _post(self, url, params, data=None, retry=2):
        params = {**params, "access_token": self.auth.access_token}
        for i in range(retry + 1):
            r = requests.post(url, params=params, data=data, timeout=60).json()
            errno = r.get("errno", 0)
            if errno == 111 and self.auth.refresh():
                params["access_token"] = self.auth.access_token
                continue
            if errno == 31034 and i < retry:           # 命中频控：退避后重试
                wait = 30 * (i + 1)
                print(f"    [频控] 接口限流，退避 {wait}s 后重试...")
                time.sleep(wait)
                continue
            return r
        return r

    # ---- 创建远程目录（父目录会自动创建；带缓存避免海量文件重复建目录） ----
    def mkdir(self, remote_dir: str, quiet: bool = False) -> None:
        if remote_dir in self._dirs_created:
            return
        self._dirs_created.add(remote_dir)  # 无论成败都记下，防止同目录反复请求
        r = self._post(self.CREATE, {}, data={"path": remote_dir, "isdir": 1, "rtype": 0})
        errno = r.get("errno", 0)
        if quiet and errno in (0, -8, 12):
            return
        if errno == 0:
            print(f"[OK] 已创建远程目录: {remote_dir}")
        elif errno in (-8, 12):
            if not quiet:
                print(f"[OK] 远程目录已存在: {remote_dir}")
        else:
            print(f"[警告] 创建远程目录失败: {errno_msg(errno)}（可能已存在，继续尝试上传）")

    # ---- 计算分片 MD5 ----
    @staticmethod
    def block_md5s(path: Path, chunk_size: int):
        """返回 (文件大小, [每片md5])，顺便流式读取避免大文件占内存"""
        md5s = []
        size = 0
        h_all = hashlib.md5()  # 仅用于本地完整性展示，不上传用
        with open(path, "rb") as f:
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                size += len(buf)
                md5s.append(hashlib.md5(buf).hexdigest())
                h_all.update(buf)
        return size, md5s, h_all.hexdigest()

    # ---- 分片上传单个分片 ----
    def _upload_part(self, remote_path, uploadid, partseq, chunk: bytes, max_retry=3):
        for attempt in range(1, max_retry + 1):
            try:
                r = requests.post(
                    self.SUPERFILE,
                    params={
                        "method": "upload",
                        "access_token": self.auth.access_token,
                        "type": "tmpfile",
                        "path": remote_path,
                        "uploadid": uploadid,
                        "partseq": partseq,
                    },
                    files={"file": (f"part{partseq}", chunk)},
                    timeout=300,
                ).json()
                if "md5" in r:
                    return r["md5"]
                print(f"    [重试{attempt}] 分片{partseq} 上传异常: {r}")
            except Exception as e:
                print(f"    [重试{attempt}] 分片{partseq} 网络异常: {e}")
            time.sleep(3 * attempt)
        raise RuntimeError(f"分片 {partseq} 连续 {max_retry} 次上传失败")

    # ---- 上传单个文件（三步：precreate -> 分片 -> create） ----
    def upload_file(self, local: Path, remote_path: str, chunk_size: int) -> bool:
        size, md5s, _ = self.block_md5s(local, chunk_size)
        block_list = json.dumps(md5s)

        # 第 1 步：预上传
        r = self._post(self.PRECREATE, {}, data={
            "path": remote_path, "size": size, "isdir": 0,
            "autoinit": 1, "rtype": 3, "block_list": block_list,
        })
        errno = r.get("errno", 0)
        if errno != 0:
            print(f"  [失败] 预上传出错: {errno_msg(errno)}")
            return False
        # 官方语义：return_type=2 秒传完成；return_type=1 需上传 block_list 指定的分片
        if r.get("return_type") == 2:
            print(f"  [秒传] 网盘已有相同文件，直接完成")
            return True

        uploadid = r["uploadid"]
        # 第 2 步：上传缺失的分片（顶层 block_list 是需要传的分片序号）
        need_parts = r.get("block_list", list(range(len(md5s))))
        total = len(need_parts)
        with open(local, "rb") as f:
            for idx, partseq in enumerate(need_parts, 1):
                f.seek(partseq * chunk_size)
                chunk = f.read(chunk_size)
                self._upload_part(remote_path, uploadid, partseq, chunk)
                print(f"    分片进度: {idx}/{total}")

        # 第 3 步：create 合并文件
        r = self._post(self.CREATE, {}, data={
            "path": remote_path, "size": size, "isdir": 0,
            "uploadid": uploadid, "rtype": 3, "block_list": block_list,
        })
        errno = r.get("errno", 0)
        if errno == 0 and ("path" in r or "fs_id" in r):
            return True
        print(f"  [失败] create 合并出错: {errno_msg(errno)} -> {r}")
        return False


# ---------------- 文件收集 ----------------
def collect_files(cfg, root: Path):
    """收集待上传文件：按配置过滤、按大小分拣。
    返回 (待上传, 超大跳过, 统计) —— 统计用于在"扫到 0 个"时说清楚到底卡在哪一步。"""
    patterns = cfg.get("exclude_patterns", ["Thumbs.db", "desktop.ini", "*.tmp", "~$*"])
    max_mb = cfg.get("max_file_size_mb", 4096)
    recursive = cfg.get("recursive", True)
    max_bytes = max_mb * 1024 * 1024
    to_upload, too_big = [], []
    stats = {"scanned": 0, "excluded": 0, "empty": 0, "subdirs": 0}

    it = root.rglob("*") if recursive else root.glob("*")
    for p in sorted(it):
        if not p.is_file():
            if p.is_dir() and p.parent == root:
                stats["subdirs"] += 1
            continue
        stats["scanned"] += 1
        # 排除脚本自身产生的记录文件
        if p.name in (DONE_LOG, TOKEN_FILE):
            continue
        rel = p.relative_to(root)
        if any(fnmatch.fnmatch(p.name, pat) or fnmatch.fnmatch(str(rel), pat)
               for pat in patterns):
            stats["excluded"] += 1
            print(f"[跳过-排除规则] {rel}")
            continue
        size = p.stat().st_size
        if size == 0:
            stats["empty"] += 1
            print(f"[跳过-空文件] {rel}")
            continue
        if size > max_bytes:
            too_big.append((p, size))
            continue
        to_upload.append((p, size))
    return to_upload, too_big, stats


def explain_empty(stats, cfg, root: Path, skipped_done: int, too_big_n: int):
    """扫到 0 个待上传文件时，把可能的原因一条条讲清楚（避免只丢一句'收工'让人一头雾水）"""
    recursive = cfg.get("recursive", True)
    print("\n" + "=" * 56)
    print("[提示] 本次没有需要上传的文件。排查信息如下：")
    print(f"  扫描模式    ：{'当前目录 + 所有子目录' if recursive else '仅当前目录下的文件'}")
    print(f"  扫到的文件数：{stats['scanned']}")
    print(f"  其中被排除  ：{stats['excluded']}   空文件：{stats['empty']}")
    print(f"  超大跳过    ：{too_big_n}   此前已上传过：{skipped_done}")
    if not recursive:
        sub = stats.get("subdirs", 0)
        deep = 0
        for _dp, _dn, fn in os.walk(root):
            deep += len(fn)
        if sub and deep > stats["scanned"]:
            print("\n  ⚠ 很可能是这里：当前只扫顶层目录，而该目录下有 "
                  f"{sub} 个子目录、里面共 {deep} 个文件。")
            print("     把「上传范围」改成【当前目录 + 所有子目录】再试。")
        elif not sub:
            print("\n  该目录下确实没有任何文件/子目录，换个本地目录试试。")
    else:
        if stats["scanned"] == 0:
            print("\n  整个目录里一个文件都没有（含子目录），换个本地目录试试。")
        elif skipped_done >= stats["scanned"]:
            print("\n  扫描到的文件此前全都上传成功过了 —— 属于正常情况，真的传完了。")
            print(f"  断点记录：{cfg.get('_cfg_dir', '.')}/uploaded_log.txt（删掉可强制重传）")
        else:
            print("\n  文件被排除规则/空文件/超大文件过滤掉了，可到配置里放宽条件。")
    print("=" * 56)


# ---------------- 上传后处理（手动确认） ----------------
def ask_action(uploaded, cfg):
    """上传成功后的处理：ask=逐批询问（默认）/ move / trash / keep"""
    mode = cfg.get("after_upload", "ask")
    if mode == "keep" or not uploaded:
        return
    if mode == "ask":
        print("\n" + "=" * 56)
        print("以下文件已成功上传到百度网盘：")
        for p, size in uploaded:
            print(f"  {human_size(size):>12}  {p}")
        print("=" * 56)
        print("如何处理这些本地文件？")
        print("  m = 移动到已完成目录 (done_dir)")
        print("  t = 放入回收站（可找回）")
        print("  k = 保留在原处不动")
        while True:
            c = input("请选择 [m/t/k]: ").strip().lower()
            if c in ("m", "t", "k"):
                mode = {"m": "move", "t": "trash", "k": "keep"}[c]
                break
            print("输入无效，请输入 m / t / k")
    if mode == "keep":
        return
    done_dir = Path(cfg.get("done_dir", "")).expanduser()
    if mode == "move" and not done_dir:
        print("[警告] 未配置 done_dir，无法移动，文件保留原处")
        return
    handle_uploaded(uploaded, mode, done_dir)


def handle_uploaded(uploaded, mode, done_dir: Path):
    if mode == "move":
        done_dir.mkdir(parents=True, exist_ok=True)
    for p, _ in uploaded:
        try:
            if mode == "move":
                dest = done_dir / p.name
                # 重名自动加序号，绝不覆盖
                i = 1
                while dest.exists():
                    dest = done_dir / f"{p.stem}({i}){p.suffix}"
                    i += 1
                shutil.move(str(p), str(dest))
                print(f"  [已移动] {p.name} -> {dest}")
            elif mode == "trash":
                if not HAS_SEND2TRASH:
                    print("  [警告] 未安装 send2trash，无法送回收站。pip install send2trash")
                    return
                send2trash(str(p))
                print(f"  [回收站] {p.name}")
        except Exception as e:
            print(f"  [错误] 处理 {p} 失败: {e}")


# ---------------- 主流程 ----------------
def build_argparser():
    ap = argparse.ArgumentParser(description="百度网盘分批上传工具（非会员友好）")
    ap.add_argument("--config", default="config.json", help="配置文件路径，默认 config.json")
    ap.add_argument("--login", action="store_true", help="首次使用：扫码授权登录（设备码模式，5分钟内有效）")
    ap.add_argument("--login-code", dest="login_code", nargs="?", const="", default=None,
                    metavar="CODE", help="授权码模式登录（备用，不限时）：不带参数会打印授权网址并提示输入授权码")
    ap.add_argument("--dry-run", action="store_true", help="干跑：只列分批计划，不上传")
    ap.add_argument("--quiet", action="store_true", help="干跑时只输出汇总统计，不逐批列出")
    ap.add_argument("--batch-size", type=int, help="临时覆盖每批文件数")
    ap.add_argument("--limit", type=int, help="本次最多上传多少个文件（测试用）")
    ap.add_argument("--yes", action="store_true",
                    help="跳过手动确认，直接按配置里的 after_upload 处理（ask 视为 keep）")
    return ap


def main():
    args = build_argparser().parse_args()
    cfg_path = Path(args.config).expanduser()
    if not cfg_path.exists():
        print(f"[错误] 找不到配置文件 {cfg_path}，请复制 config.example.json 为 config.json 并填写")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

    # 相对路径统一相对 config 文件所在目录解析，避免受“从哪里启动”影响
    def resolve_dir(key):
        v = cfg.get(key, "")
        if v and not Path(v).is_absolute():
            cfg[key] = str((cfg_path.parent / v).resolve())
    for k in ("local_dir", "done_dir"):
        resolve_dir(k)

    # —— 登录 / 加载 Token ——
    auth = BaiduAuth(cfg["app_key"], cfg["secret_key"], cfg_path.parent / TOKEN_FILE)
    if args.login:
        auth.login()
        return
    if args.login_code is not None:
        auth.login_by_code(args.login_code)
        return
    # 干跑不需要 Token，没登录也能先预览分批计划
    if not args.dry_run:
        auth.ensure_token()
        api = PanApi(auth)

    root = Path(cfg["local_dir"]).expanduser()
    if not root.is_dir():
        print(f"[错误] 本地目录不存在: {root}")
        sys.exit(1)

    done_log_path = cfg_path.parent / DONE_LOG
    done_log = set(load_json(done_log_path, []))
    # 兼容新版追加式文本日志（一行一个已上传文件路径）
    done_log_txt = done_log_path.with_suffix(".txt")
    if done_log_txt.exists():
        done_log.update(ln for ln in
                        done_log_txt.read_text(encoding="utf-8-sig").splitlines() if ln)

    print(f"扫描本地目录: {root}")
    print(f"扫描模式: {'当前目录 + 所有子目录'
                     if cfg.get('recursive', True) else '仅当前目录下的文件（不含子目录）'}")
    cfg["_cfg_dir"] = str(cfg_path.parent)
    to_upload, too_big, stats = collect_files(cfg, root)

    # 超大文件清单：宁可跳过也不浪费上传时间
    if too_big:
        print("\n[!] 以下文件超过大小上限，本次跳过（可在 config 里调 max_file_size_mb）：")
        for p, size in too_big:
            print(f"    {human_size(size):>12}  {p}")

    # 断点续传：跳过历史已成功文件
    pending = [(p, s) for p, s in to_upload if str(p) not in done_log]
    skipped = len(to_upload) - len(pending)
    if skipped:
        print(f"\n[断点] 检测到 {skipped} 个文件此前已成功上传，本次自动跳过")
    if args.limit:
        pending = pending[:args.limit]

    batch_size = args.batch_size or cfg.get("batch_size", 50)
    batch_pause = cfg.get("batch_pause_sec", 15)
    file_interval = cfg.get("file_interval_sec", 0.5)
    chunk_size = cfg.get("chunk_size_mb", DEFAULT_CHUNK_MB) * 1024 * 1024
    remote_base = cfg["remote_dir"].rstrip("/")

    # 干跑：只出计划
    if args.dry_run:
        total_size = sum(s for _, s in pending)
        print(f"\n[干跑] 扫描 {stats['scanned']} 个文件 → 排除 {stats['excluded']} / "
              f"空文件 {stats['empty']} / 超大 {len(too_big)} / 已传过 {skipped} / "
              f"待上传 {len(pending)}")
        if not args.quiet:
            print(f"\n[干跑] 共 {len(pending)} 个待上传文件，按每批 {batch_size} 个分组，批间暂停 {batch_pause}s：")
            for i in range(0, len(pending), batch_size):
                batch = pending[i:i + batch_size]
                print(f"  —— 第 {i//batch_size + 1} 批（{len(batch)} 个文件, "
                      f"{human_size(sum(s for _, s in batch))}）——")
                for p, s in batch[:5]:
                    print(f"     {human_size(s):>10}  {p.name}")
                if len(batch) > 5:
                    print(f"     ... 其余 {len(batch)-5} 个略")
        print(f"\n[干跑] 合计 {len(pending)} 个文件, 总大小 {human_size(total_size)}，"
              f"分 {(len(pending)+batch_size-1)//batch_size} 批")
        if not pending:
            explain_empty(stats, cfg, root, skipped, len(too_big))
        return

    if not pending:
        explain_empty(stats, cfg, root, skipped, len(too_big))
        return

    # 进程锁：防止网页重启后重复起第二个上传进程（会导致同一批文件被传两遍）
    got, owner = acquire_pidfile(cfg_path.parent / PID_FILE)
    if not got:
        print(f"[提示] 已有一个上传进程在跑（PID {owner}），本次不再重复启动。")
        print("      要换参数重传，先关掉它：网页点「停止」，或任务管理器结束该 PID。")
        return

    print(f"\n开始上传：共 {len(pending)} 个文件，每批 {batch_size} 个，批间暂停 {batch_pause} 秒")
    print("(提示：Ctrl+C 可随时中断，已成功的文件下次运行会自动跳过)\n")

    api.mkdir(remote_base)
    all_uploaded, failed = [], []
    n_batch = (len(pending) + batch_size - 1) // batch_size
    # 断点记录改为追加写文本（一行一个路径），海量文件下比整份 JSON 重写快得多
    done_log_fh = open(done_log_path.with_suffix(".txt"), "a", encoding="utf-8")

    # 进度状态文件（供网页控制面板轮询）：原子写，随时中断不损坏
    progress_path = cfg_path.parent / "progress.json"

    def write_progress(current: str = "", finished: bool = False):
        data = {"total": len(pending), "done": len(all_uploaded),
                "failed": len(failed), "current": current,
                "finished": finished, "time": time.strftime("%H:%M:%S")}
        tmp = progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, progress_path)

    write_progress()

    for bi in range(n_batch):
        batch = pending[bi * batch_size:(bi + 1) * batch_size]
        print(f"\n===== 第 {bi+1}/{n_batch} 批（{len(batch)} 个文件）=====")
        for p, size in batch:
            rel = p.relative_to(root)
            remote_path = f"{remote_base}/{str(rel).replace(chr(92), '/')}"
            # 远程父目录不存在则先创建（带缓存：同一目录只请求一次）
            remote_parent = remote_path.rsplit("/", 1)[0]
            if remote_parent != remote_base:
                api.mkdir(remote_parent, quiet=True)
            print(f"-> 上传: {rel} ({human_size(size)})")
            ok = False
            try:
                ok = api.upload_file(p, remote_path, chunk_size)
            except Exception as e:
                print(f"  [失败] 异常: {e}")
            if ok:
                all_uploaded.append((p, size))
                done_log.add(str(p))
                done_log_fh.write(str(p) + "\n")   # 追加写一行，避免海量文件反复序列化整个清单
                done_log_fh.flush()
            else:
                failed.append((p, size))
            write_progress(str(rel))
            time.sleep(file_interval)  # 单文件间隔，降低频控风险

        # 批间暂停：模拟非会员"手动分次上传"，避开数量限制
        if bi < n_batch - 1:
            print(f"\n[暂停] 批间等待 {batch_pause} 秒（避开非会员上传数量限制）...")
            time.sleep(batch_pause)

    # 汇总
    done_log_fh.close()
    write_progress(finished=True)
    print("\n" + "=" * 56)
    print(f"上传完成：成功 {len(all_uploaded)} / 失败 {len(failed)} / 跳过(超大) {len(too_big)}")
    if failed:
        print(f"失败 {len(failed)} 个（下次运行会自动重试），前 20 个：")
        for p, s in failed[:20]:
            print(f"    {p}  ({human_size(s)})")
    print("=" * 56)

    # 上传成功文件的后处理
    if args.yes and cfg.get("after_upload", "ask") == "ask":
        print("\n[--yes] 跳过手动确认，文件保留原处。")
        return
    ask_action(all_uploaded, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[中断] 用户取消。已成功的文件已记录，下次运行自动续传。")
        sys.exit(130)
