#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度网盘上传工具 - 本地网页控制面板
浏览器里完成：参数配置 / 授权登录 / 开始停止上传 / 实时进度

只绑定 127.0.0.1，外部无法访问。启动后自动打开浏览器。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import preview_store                # 计划预览的本地存盘（同目录模块）

PROJ = Path(__file__).resolve().parent
PY = sys.executable                      # 用当前解释器跑上传子进程
CONFIG = PROJ / "config.json"
TOKEN = PROJ / "token.json"
LOGFILE = PROJ / "upload.log"
PROGRESS = PROJ / "progress.json"
HTMLFILE = PROJ / "webui.html"
PIDFILE = PROJ / "upload.pid"            # 上传进程锁：面板重启也能认出还在跑的上传
LOCAL_RENAME_LOG = PROJ / "local_rename_log.jsonl"   # 本地改名记录（可整批回退）
PORT = 8765
LOG_MAX_BYTES = 20 * 1024 * 1024         # 日志超过 20MB 就归档，避免长期任务撑爆磁盘

# 预览文件标题用的人话。前端也有一份同名的，但存进文件里的标题得由后端写，
# 否则列表在别的地方（比如直接翻 previews/ 目录）看就是一堆代号
RENAME_MODE_LABELS = {"replace": "查找替换", "affix": "加前后缀", "serial": "序号重命名",
                      "regex": "正则替换", "clean": "去广告清理", "title": "只保留书名"}
ORGANIZE_LABELS = {"category": "按大类", "ext": "按后缀", "date": "按修改月份"}


def filter_label(f):
    """把删除的筛选条件写成一句人能读的话，用作预览文件标题"""
    f = f or {}
    bits = []
    if f.get("exts"):
        bits.append("后缀 " + str(f["exts"]))
    if f.get("keywords"):
        bits.append("含 " + str(f["keywords"]))
    if f.get("regex"):
        bits.append("正则 " + str(f["regex"]))
    if f.get("min_mb"):
        bits.append(f"大于 {f['min_mb']}MB")
    if f.get("max_mb"):
        bits.append(f"小于 {f['max_mb']}MB")
    return "、".join(bits) or "无条件（全部文件）"

# ---------------- 上传子进程管理 ----------------
_state = {"proc": None, "lock": threading.Lock()}


def _base_env():
    return {**os.environ, "PYTHONPATH": str(PROJ / "libs")}


def rotate_log():
    """upload.log 超过阈值就归档成 upload.log.1，只留一份旧档"""
    try:
        if LOGFILE.exists() and LOGFILE.stat().st_size > LOG_MAX_BYTES:
            old = PROJ / "upload.log.1"
            if old.exists():
                old.unlink()
            LOGFILE.rename(old)
    except Exception:
        pass    # 轮转失败不影响上传本身


def pid_alive(pid: int) -> bool:
    """判断 PID 是否还活着（Windows 用 OpenProcess，避开 os.kill 的语义差异）"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
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


def read_pidfile() -> int:
    """返回锁文件里记录的、且确实活着的上传进程 PID（没有则 0）"""
    try:
        if not PIDFILE.exists():
            return 0
        pid = int(PIDFILE.read_text(encoding="utf-8").strip())
        return pid if pid_alive(pid) else 0
    except Exception:
        return 0


def kill_pid(pid: int) -> bool:
    """按 PID 结束上传进程（Windows 用 taskkill，带子进程一起收）"""
    try:
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            return r.returncode == 0
        os.kill(pid, 15)
        return True
    except Exception:
        return False


def start_upload(limit: int = 0):
    with _state["lock"]:
        p = _state["proc"]
        if p is not None and p.poll() is None:
            return False, "已有上传任务在运行中"
        # 面板重启过、内存里没记录时，靠 PID 锁文件认出"上一个上传还在跑"
        other = read_pidfile()
        if other:
            return False, f"已有上传任务在运行中（PID {other}）"
        cmd = [PY, "-X", "utf8", "-u", str(PROJ / "upload_baidu.py")]
        if limit and limit > 0:
            cmd += ["--limit", str(limit)]
        cmd += ["--yes"]   # 无交互环境，跳过末尾手动确认（after_upload 用 move/trash/keep）
        rotate_log()       # 上一轮日志太大就先归档，本次从新文件开始写
        f = open(LOGFILE, "a", encoding="utf-8")
        f.write(f"\n==== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        f.flush()
        _state["proc"] = subprocess.Popen(
            cmd, cwd=str(PROJ), env=_base_env(), stdout=f, stderr=subprocess.STDOUT)
        return True, "上传已启动"


def stop_upload():
    with _state["lock"]:
        p = _state["proc"]
        if p is not None and p.poll() is None:
            p.terminate()
            return True, "停止信号已发送，进度已保存，可随时续传"
        # 面板重启后 memory 里没进程对象，改用 PID 锁文件来停
        other = read_pidfile()
        if other and kill_pid(other):
            try:
                PIDFILE.unlink()
            except Exception:
                pass
            return True, f"已停止上传进程（PID {other}），进度已保存，可随时续传"
        return False, "当前没有运行中的上传任务"


def is_running():
    if read_pidfile():          # 认得出来别的实例/上次启动留下的上传进程
        return True
    p = _state["proc"]
    return p is not None and p.poll() is None


def tail_text(path: Path, max_chars: int = 6000, max_lines: int = 60):
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        out = "\n".join(lines)
        return out[-max_chars:]
    except Exception:
        return ""


def read_config():
    """读 config.json；文件缺失或坏了就返回空字典（调用方自己给出可读的提示）"""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


# ---------------- 授权相关 ----------------
def do_login(code: str):
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    r = requests.post("https://openapi.baidu.com/oauth/2.0/token", params={
        "grant_type": "authorization_code", "code": code,
        "client_id": cfg["app_key"], "client_secret": cfg["secret_key"],
        "redirect_uri": "oob",
    }, timeout=15).json()
    if "access_token" in r:
        TOKEN.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, "授权成功，Token 已保存"
    return False, f"换取 Token 失败：{r.get('error_description', r)}"


def auth_url():
    """带 AppKey 的授权网址；AppKey 没配好时退回开放平台控制台"""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        cfg = {}
    key = str(cfg.get("app_key", "")).strip()
    if not key or key in ("xxx", "test", "YOUR_APP_KEY", "你的AppKey") or key.startswith("你的"):
        return "https://pan.baidu.com/union/console"
    return ("https://openapi.baidu.com/oauth/2.0/authorize?"
            f"response_type=code&client_id={key}"
            "&redirect_uri=oob&scope=basic,netdisk")


def verify_token():
    if not TOKEN.exists():
        return False, "尚未授权"
    t = json.loads(TOKEN.read_text(encoding="utf-8-sig"))
    at = t.get("access_token", "")
    try:
        u = requests.get("https://pan.baidu.com/rest/2.0/xpan/nas",
                         params={"method": "uinfo", "access_token": at}, timeout=15).json()
        if u.get("errno") != 0:
            return False, f"Token 无效：{u}"
        q = requests.get("https://pan.baidu.com/api/quota",
                         params={"access_token": at}, timeout=15).json()
        used = q.get("used", 0) / 1024**3
        total = q.get("total", 0) / 1024**3
        return True, (f"{u.get('baidu_name', '?')}（{'会员' if u.get('vip_type') else '普通用户'}），"
                      f"网盘已用 {used:.0f}GB / {total:.0f}GB")
    except Exception as e:
        return False, f"验证请求失败：{e}"


# ---------------- 网盘扫描进度（生成预览时前端轮询） ----------------
# 扫描跑在 /api/pan_plan（或 /api/pan_export）的请求线程里，进度由另一个请求
# 线程读取，所以用锁保护。ThreadingHTTPServer 保证两边互不阻塞。
_scan_lock = threading.Lock()
_scan = {"running": False, "phase": "", "dirs": 0, "files": 0, "path": "",
         "t0": 0.0, "stopping": False, "cancelled": False}
# 「停止」按钮：前端点一下就把这个 Event 置位，扫描线程在每个检查点自己中断。
# 只能由 Event 通知、不能在别的线程里杀线程——扫描是纯只读的，让它自己退出最安全。
_scan_cancel = threading.Event()


def scan_begin():
    _scan_cancel.clear()            # 新一轮扫描开始，清掉上一轮遗留的取消请求
    with _scan_lock:
        _scan.update(running=True, phase="扫描目录", dirs=0, files=0, path="",
                     t0=time.time(), stopping=False, cancelled=False)


def scan_tick(dirs, files, cur):
    """list_dir 每列完一个目录回调一次"""
    with _scan_lock:
        _scan.update(dirs=dirs, files=files, path=cur)


def scan_phase(phase):
    with _scan_lock:
        _scan["phase"] = phase


def scan_request_stop():
    """前端点「停止」：只置标志，真正中断由扫描线程在下一个检查点完成。
    返回 False 表示当前没有正在跑的扫描（可能是刚结束，前端据此提示）。"""
    with _scan_lock:
        if not _scan["running"]:
            return False
        _scan["stopping"] = True        # 让前端能显示「正在停止…」
    _scan_cancel.set()
    return True


def scan_should_stop():
    return _scan_cancel.is_set()


def scan_end(cancelled=False):
    _scan_cancel.clear()               # 扫描已退出，清掉标志，避免影响下一次
    with _scan_lock:
        _scan.update(running=False, stopping=False, cancelled=bool(cancelled))


def scan_snapshot():
    with _scan_lock:
        snap = dict(_scan)
    snap["elapsed"] = round(time.time() - snap["t0"], 1) if snap.get("t0") else 0
    snap.pop("t0", None)
    return snap


# ---------------- 执行进度（点「执行」时前端轮询） ----------------
# 执行同样跑在请求线程里，进度由轮询线程读，所以一样用锁保护。
# 不同于扫描的是：执行的总条数事先就知道，所以前端能显示真实百分比。
_exec_lock = threading.Lock()
_exec = {"running": False, "phase": "", "done": 0, "total": 0, "fails": 0,
         "t0": 0.0}


def exec_begin(total=0):
    with _exec_lock:
        _exec.update(running=True, phase="准备中", done=0, total=int(total or 0),
                     fails=0, t0=time.time())


def exec_tick(snap):
    """apply_plan 每完成一批回调一次，snap={done,total,phase,fails}"""
    with _exec_lock:
        _exec.update(done=snap.get("done", 0) or 0,
                     total=snap.get("total", 0) or 0,
                     phase=snap.get("phase", "") or "",
                     fails=snap.get("fails", 0) or 0)


def exec_end():
    with _exec_lock:
        _exec.update(running=False)


def exec_snapshot():
    with _exec_lock:
        snap = dict(_exec)
    snap["elapsed"] = round(time.time() - snap["t0"], 1) if snap.get("t0") else 0
    snap.pop("t0", None)
    return snap


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a):   # 静默访问日志
        pass

    def end_headers(self):
        # 允许 file:// 直接双击打开的页面也调用本机 API（服务只绑 127.0.0.1，外部访问不到）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]   # 去掉可能的查询串
        if path in ("/", "/index.html", "/webui.html"):
            body = HTMLFILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/config":
            try:
                cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
                # 顺带回传「有没有可回退的本地改名」，前端好决定撤销按钮亮不亮
                undo = {}
                try:
                    import local_rename
                    rec = local_rename.last_undoable(LOCAL_RENAME_LOG)
                    if rec:
                        undo = {"available": True, "ts": rec.get("ts"),
                                "count": len(rec.get("items") or [])}
                except Exception:
                    pass
                self._json({"ok": True, "config": cfg, "rename_undo": undo})
            except Exception as e:
                self._json({"ok": False, "msg": f"读取配置失败：{e}"})
        elif path == "/api/progress":
            prog = {}
            if PROGRESS.exists():
                try:
                    prog = json.loads(PROGRESS.read_text(encoding="utf-8-sig"))
                except Exception:
                    pass
            self._json({"ok": True, "running": is_running(),
                        "pid": read_pidfile(),
                        "progress": prog, "log": tail_text(LOGFILE)})
        elif path == "/api/authurl":
            # 顺带回传 Token 有效期，页面上好显示"已授权到哪天"，刷新页面不用重新授权
            info = {}
            if TOKEN.exists():
                try:
                    t = json.loads(TOKEN.read_text(encoding="utf-8-sig"))
                    exp = int(t.get("expires_in", 0) or 0)
                    saved = TOKEN.stat().st_mtime
                    info = {
                        "saved_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(saved)),
                        "expires_at": (time.strftime("%Y-%m-%d", time.localtime(saved + exp))
                                       if exp else ""),
                    }
                except Exception:
                    pass
            self._json({"ok": True, "url": auth_url(), "has_token": TOKEN.exists(),
                        "token_info": info})
        elif path == "/api/authgo":
            # 302 直接跳转：浏览器原生导航，不受弹窗拦截器影响
            url = auth_url()
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/api/"):
            # 用 GET 调用的接口（如 /api/verify、/api/stop、/api/dryrun）一律放行
            self.handle_api(path, {})
        else:
            self._json({"ok": False, "msg": f"接口不存在：{path}"}, 404)

    def handle_api(self, path, body):
        """所有 /api/* 路由统一在这里处理：GET（无 body）和 POST 都走这条路，
        避免前端用错 HTTP 方法时出现 404 not found 却不知所措。"""
        if path == "/api/config":
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._json({"ok": False, "msg": "配置格式错误"})
            # 基本字段校验
            for k in ("app_key", "secret_key", "local_dir", "remote_dir"):
                if not str(cfg.get(k, "")).strip():
                    return self._json({"ok": False, "msg": f"缺少必填项：{k}"})
            for k in ("batch_size", "batch_pause_sec", "file_interval_sec",
                      "max_file_size_mb", "workers"):
                try:
                    cfg[k] = int(cfg[k])
                except Exception:
                    return self._json({"ok": False, "msg": f"{k} 必须是数字"})
            cfg["recursive"] = bool(cfg.get("recursive", True))
            cfg["chunk_size_mb"] = 4
            # after_upload 只允许三种（网页端无交互，不支持 ask）
            if cfg.get("after_upload") not in ("keep", "move", "trash"):
                cfg["after_upload"] = "keep"
            # upload_layout（网盘目录布局）只允许四种，防手改 config.json 写错
            if cfg.get("upload_layout") not in ("mirror", "flat", "by_category", "by_ext"):
                cfg["upload_layout"] = "mirror"
            # pre_rename（上传前本地改名方式）只允许三种；默认不改名最安全
            if cfg.get("pre_rename") not in ("none", "title", "clean"):
                cfg["pre_rename"] = "none"
            if not str(cfg.get("done_dir", "")).strip():
                cfg["done_dir"] = str(Path(cfg["local_dir"]).parent / "已上传")
            CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            return self._json({"ok": True, "msg": "配置已保存"})

        if path == "/api/start":
            limit = int(body.get("limit", 0) or 0)
            ok, msg = start_upload(limit)
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/stop":
            ok, msg = stop_upload()
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/dryrun":
            if is_running():
                return self._json({"ok": False, "msg": "上传进行中，不能干跑"})
            try:
                r = subprocess.run(
                    [PY, "-X", "utf8", str(PROJ / "upload_baidu.py"), "--dry-run", "--quiet"],
                    cwd=str(PROJ), env=_base_env(), capture_output=True,
                    text=True, encoding="utf-8", timeout=600)
                out = (r.stdout or "") + (r.stderr or "")
                return self._json({"ok": r.returncode == 0, "output": out[-8000:]})
            except subprocess.TimeoutExpired:
                return self._json({"ok": False, "msg": "干跑超时（目录太大？）"})

        if path == "/api/login":
            code = str(body.get("code", "")).strip()
            if not code:
                return self._json({"ok": False, "msg": "请填写授权码"})
            ok, msg = do_login(code)
            return self._json({"ok": ok, "msg": msg})

        if path == "/api/verify":
            ok, msg = verify_token()
            return self._json({"ok": ok, "msg": msg})

        # ---------------- 网盘整理 / 批量改名 / 批量删除（沙盒内） ----------------
        if path in ("/api/pan_browse", "/api/pan_plan", "/api/pan_apply",
                    "/api/pan_export", "/api/pan_validate"):
            return self._handle_pan(path, body)

        # 扫描进度：前端在「生成预览 / 导出清单」期间轮询。
        # 不放上面那组里——这里不需要构造 PanFiles，也就不该因未授权而失败。
        if path == "/api/pan_plan_progress":
            return self._json({"ok": True, **scan_snapshot()})

        # 执行进度：点「执行」期间前端轮询（同上，不需要构造 PanFiles）
        if path == "/api/pan_apply_progress":
            return self._json({"ok": True, **exec_snapshot()})

        # 停止扫描：同样是纯内存操作，不碰网盘
        if path == "/api/pan_cancel":
            stopping = scan_request_stop()
            return self._json({
                "ok": True, "stopping": stopping,
                "msg": "正在停止…" if stopping else "当前没有正在进行的扫描"})

        # ---------------- 预览文件（本地存盘，避免反复重扫网盘）----------------
        # 这一组都不需要构造 PanFiles（除了 preview_load 要拿沙盒路径做越界校验），
        # 所以放在 pan_* 那组外面，未授权时也能看列表
        if path == "/api/preview_list":
            return self._json({"ok": True, "items": preview_store.list_previews(PROJ),
                               "dir": preview_store.DIR_NAME,
                               "max_keep": preview_store.MAX_KEEP})
        if path == "/api/preview_load":
            return self._preview_load(body)
        if path == "/api/preview_delete":
            pid = str(body.get("id") or "")
            ok = preview_store.delete_preview(PROJ, pid)
            return self._json({"ok": ok, "id": pid,
                               "msg": "预览文件已删除" if ok else "找不到这份预览文件"})
        if path == "/api/preview_clean":
            n = preview_store.clean_executed(PROJ)
            return self._json({"ok": True, "cleaned": n,
                               "msg": f"已清理 {n} 份执行过的预览文件"})

        # ---------------- 本地改名（上传前预处理，动的是磁盘上的真文件）----------------
        if path == "/api/local_rename_preview":
            return self._local_rename_preview(body)
        if path == "/api/local_rename_apply":
            return self._local_rename_apply(body)
        if path == "/api/local_rename_undo":
            return self._local_rename_undo()

        return self._json({"ok": False, "msg": f"接口不存在：{path}"}, 404)

    # ---------------------------------------------------------------
    # 预览文件：复用 / 载入 / 删除
    # ---------------------------------------------------------------
    @staticmethod
    def _preview_resp(rec, reused=False):
        """把一份预览记录翻成与 /api/pan_plan 完全同形的响应

        同形是关键：前端不必为「新扫的」和「复用的」写两条渲染路径。
        info 里存的是那次扫描的统计（scanned / unchanged / auto / nested …）。
        """
        resp = {"ok": True, "ops": rec.get("ops") or [],
                "total": int(rec.get("total") or 0),
                "preview_id": rec.get("id"), "preview_ts": rec.get("ts", ""),
                "preview_age": int(max(0, time.time() - (rec.get("t0") or 0))),
                "preview_kind": rec.get("kind", ""),
                "preview_executed": bool(rec.get("executed")),
                "reused": bool(reused)}
        resp.update(rec.get("info") or {})
        return resp

    def _preview_load(self, body):
        """载入一份存下来的预览，准备执行

        **必须重新校验一遍路径**：预览文件就是磁盘上的普通 JSON，能被手改，也可能
        是换了 remote_dir 之前生成的。拿一份越界的清单去执行，后果比重新扫一次严重
        得多，所以这里一律过 pan_tools.validate_plan。
        """
        import pan_tools
        rec = preview_store.load_preview(PROJ, str(body.get("id") or ""))
        if not rec:
            return self._json({"ok": False, "msg": "找不到这份预览文件（可能已被清理）"})
        try:
            _, sandbox = self._pan()
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)})
        good, errs = pan_tools.validate_plan({"ops": rec.get("ops") or []}, sandbox)
        if not good and errs:
            return self._json({"ok": False,
                               "msg": "这份预览已不可用：" + "；".join(errs[:3])})
        resp = self._preview_resp(rec, reused=True)
        # 用**原始** ops 而不是校验后的 good：校验会把 name / auto / isdir 这些
        # 只用于展示的字段抹掉，预览区就分不出「空文件夹」和「普通文件」了。
        # 真正的把关在执行那一步——/api/pan_apply 会再校验一次
        resp["total"] = len(rec.get("ops") or [])
        if errs:
            resp["errors"] = errs[:8]
        resp["msg"] = (f"已载入 {rec.get('ts', '')} 生成的预览（{resp['total']} 条）"
                       + (f"，其中 {len(errs)} 条会在执行时被拦下" if errs else ""))
        return self._json(resp)

    # ---------------------------------------------------------------
    # 本地改名：预览 → 执行 → （改错了）回退
    # ---------------------------------------------------------------
    def _local_rename_preview(self, body):
        import local_rename
        cfg = read_config()
        if not str(cfg.get("local_dir") or "").strip():
            return self._json({"ok": False, "msg": "请先在「目录」里填好本地上传目录"})
        mode = str(body.get("mode") or "title")
        try:
            files, skipped = local_rename.scan_local(cfg)
        except FileNotFoundError as e:
            return self._json({"ok": False, "msg": str(e)})
        except Exception as e:
            return self._json({"ok": False, "msg": f"扫描失败：{e}"})
        rows, unchanged = local_rename.plan_local(files, mode, body.get("params") or {})
        ops = [{"op": "rename", "name": r["name"], "newname": r["newname"],
                "path": r["path"], "auto": r.get("auto", False)} for r in rows]
        return self._json({
            "ok": True, "ops": ops[:2000], "total": len(ops),
            "unchanged": unchanged, "scanned": len(files),
            "auto": sum(1 for r in rows if r.get("auto")),
            "skipped": skipped, "mode_label": local_rename.MODE_LABELS.get(mode, mode),
            "dir": str(cfg.get("local_dir"))})

    def _local_rename_apply(self, body):
        import local_rename
        # 上传正在跑时改名会让脚本对不上号（它按路径记录已传清单），必须先停
        if is_running():
            return self._json({"ok": False, "msg": "上传进行中，请先停止再改名"})
        cfg = read_config()
        if not str(cfg.get("local_dir") or "").strip():
            return self._json({"ok": False, "msg": "请先填好本地上传目录"})
        mode = str(body.get("mode") or "title")
        try:
            files, skipped = local_rename.scan_local(cfg)
        except Exception as e:
            return self._json({"ok": False, "msg": f"扫描失败：{e}"})
        rows, _ = local_rename.plan_local(files, mode, body.get("params") or {})
        if not rows:
            return self._json({"ok": True, "renamed": 0, "fails": [], "skipped": skipped,
                               "msg": "没有需要改名的文件"})
        done, fails = local_rename.apply_local(rows, LOCAL_RENAME_LOG)
        return self._json({"ok": True, "renamed": len(done), "fails": fails[:50],
                           "fail_count": len(fails), "skipped": skipped,
                           "auto": sum(1 for r in rows if r.get("auto")),
                           "sample": done[:40],
                           "log": LOCAL_RENAME_LOG.name})

    def _local_rename_undo(self):
        import local_rename
        if is_running():
            return self._json({"ok": False, "msg": "上传进行中，请先停止再回退"})
        done, fails, rec = local_rename.undo_last(LOCAL_RENAME_LOG)
        if rec is None:
            return self._json({"ok": False, "msg": "没有可回退的改名记录"})
        return self._json({"ok": True, "reverted": len(done), "fails": fails[:50],
                           "fail_count": len(fails), "ts": rec.get("ts"),
                           "sample": done[:40]})

    # ---------------------------------------------------------------
    def _pan(self):
        """构造网盘操作对象（每次请求都重新读 token/config，保证拿到最新值）"""
        import pan_tools
        tk = json.loads(TOKEN.read_text(encoding="utf-8-sig")) if TOKEN.exists() else {}
        if not tk.get("access_token"):
            raise RuntimeError("还没有授权，请先在上方完成百度账号授权")
        cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.exists() else {}
        sandbox = str(cfg.get("remote_dir") or "/apps/baidu_uploader").rstrip("/")
        return pan_tools.PanFiles(tk["access_token"], sandbox), sandbox

    def _handle_pan(self, path, body):
        import pan_tools
        try:
            pan, sandbox = self._pan()
        except Exception as e:
            return self._json({"ok": False, "msg": str(e)})

        # 浏览目录：返回子目录 + 该层文件（限制条数，防止 8 万文件卡死页面）
        if path == "/api/pan_browse":
            p = str(body.get("path") or sandbox).strip() or sandbox
            if p != sandbox and not p.startswith(sandbox + "/"):
                p = sandbox
            try:
                items = pan.list_dir(p, recursive=False)
            except Exception as e:
                # e 已是 pan_tools 给的人话（含路径），别再套一层前缀
                return self._json({"ok": False, "msg": str(e)})
            dirs = sorted([i for i in items if i["isdir"]], key=lambda x: x["name"])
            files = sorted([i for i in items if not i["isdir"]], key=lambda x: x["name"])
            return self._json({"ok": True, "path": p, "sandbox": sandbox,
                               "dirs": dirs, "files": files[:500],
                               "file_total": len(files),
                               "truncated": len(files) > 500})

        # 生成计划（预览，不执行）
        if path == "/api/pan_plan":
            kind = str(body.get("kind") or "")
            p = str(body.get("path") or sandbox).strip() or sandbox
            recursive = bool(body.get("recursive", False))
            refresh = bool(body.get("refresh"))     # 「重新生成」= 忽略缓存，强制重扫

            # 把「影响结果的全部输入」归一化：它既是判断「同参数」的指纹依据（决定能
            # 不能复用旧预览），也随预览文件一起存下来，事后能看清这份计划是拿什么算的
            payload = {"path": p, "recursive": recursive}
            for k in ("mode", "params", "by", "dest", "filters", "skip"):
                if k in body:
                    payload[k] = body[k]
            if kind == "empty_dir":
                payload["recursive"] = True         # 后端强制递归，指纹按实际口径算

            # ① 同参数的老预览还在 → 直接复用，一个网盘请求都不发。这就是这个功能的
            #    主要目的：反复调参反复看预览时，不该每次都把几千个目录重走一遍
            if not refresh:
                old = preview_store.find_recent(
                    PROJ, preview_store.fingerprint(kind, payload, sandbox))
                if old:
                    return self._json(self._preview_resp(old, reused=True))

            # 递归扫描可能是几千个目录的活儿，把进度摊给前端轮询；
            # should_stop 让「停止」按钮能在下一个检查点把它刹住（只读，无副作用）
            scan_begin()
            cancelled = False
            entries = None
            # 列不开的子目录（百度脏条目）：不中断整次扫描，但也绝不能装作没看见——
            # 结果不完整这件事必须传到前端，否则用户会以为「就这些文件」
            failures = []
            try:
                if kind == "empty_dir":
                    # 要判定「目录里有没有文件」就得看到目录本身，list_files 把它们
                    # 滤掉了；而且必须递归——只看一层根本不知道子目录里是不是还有文件。
                    # 所以这里无视前端的 recursive 参数，一律按递归扫。
                    entries = pan.list_dir(p, recursive=True, on_progress=scan_tick,
                                           should_stop=scan_should_stop,
                                           failures=failures)
                    files = [e for e in entries if not e["isdir"]]
                else:
                    files = pan.list_files(p, recursive=recursive, on_progress=scan_tick,
                                           should_stop=scan_should_stop,
                                           failures=failures)
            except pan_tools.ScanCancelled as e:
                cancelled = True
                return self._json({"ok": False, "cancelled": True, "msg":
                                   f"已停止扫描（已遍历 {e.dirs} 个目录 / "
                                   f"找到 {e.files} 个文件），没有做任何改动"})
            except Exception as e:
                # PanError 的文案已经是给人看的（含路径和原因），别再套一层前缀
                m = str(e) if isinstance(e, pan_tools.PanError) else f"扫描失败：{e}"
                return self._json({"ok": False, "msg": m})
            finally:
                scan_end(cancelled=cancelled)
            scan_phase("生成计划")

            # 每种计划各自的统计信息（info）与标题（label）。info 会被原样塞回响应
            # 并存进预览文件，所以键名必须和前端读的一致（scanned/unchanged/auto…）
            if kind == "rename":
                mode = str(body.get("mode") or "replace")
                ops, unchanged, auto = pan_tools.build_rename_plan(
                    files, mode, body.get("params") or {})
                info = {"unchanged": len(unchanged), "auto": auto, "scanned": len(files)}
                label = "批量改名·" + RENAME_MODE_LABELS.get(mode, mode)
            elif kind == "organize":
                dest = str(body.get("dest") or "").strip()
                if not dest:
                    return self._json({"ok": False, "msg": "请填写归档目标目录"})
                by = str(body.get("by") or "category")
                ops = pan_tools.build_organize_plan(files, by, dest, sandbox)
                info = {"scanned": len(files)}
                label = "整理归档·" + ORGANIZE_LABELS.get(by, by) + " → " + dest
            elif kind == "delete":
                flt = body.get("filters") or {}
                ops = pan_tools.build_delete_plan(files, flt)
                info = {"scanned": len(files)}
                label = "批量删除·" + filter_label(flt)
            elif kind == "empty_dir":
                # skip 由前端给，默认保护本工具自己的备份区；
                # unreadable 是这次扫不进去的目录——「不知道里面有什么」绝不能
                # 当成「里面什么都没有」，那会删掉用户的真数据
                ops, einfo = pan_tools.build_empty_dir_plan(
                    entries, p, skip=str(body.get("skip") or "_覆盖备份"),
                    unreadable=[f["path"] for f in failures])
                info = {"scanned": len(files), **einfo}
                label = "空文件夹"
            else:
                return self._json({"ok": False, "msg": f"未知的计划类型：{kind}"})

            # 有目录没扫进去 ⇒ 这次结果是不完整的。带上前 30 个明细，让用户能自己去看
            if failures:
                info["skipped_total"] = len(failures)
                info["skipped_dirs"] = failures[:30]

            full = len(ops)
            ops = ops[:2000]        # 与执行上限一致：再多也执行不了，不必塞进预览文件
            # 标题带上目录和扫描范围：下拉框里两份「批量改名·只保留书名」如果只有
            # 条数不同，根本认不出哪份是含子目录的、哪份是只扫本级的
            scope = "含子目录" if payload["recursive"] else "仅本级"
            # ② 落盘成预览文件。存失败（比如磁盘满）不影响本次预览——只是没了缓存
            rec = preview_store.save_preview(
                PROJ, kind, ops, {"total": full, **info}, payload,
                sandbox=sandbox, path=p, label=f"{label} · {p}（{scope}）")
            resp = {"ok": True, "ops": ops, "total": full, "reused": False, **info}
            if rec:
                resp.update(preview_id=rec["id"], preview_ts=rec["ts"], preview_age=0)
            return self._json(resp)

        # 执行计划
        if path == "/api/pan_apply":
            ops = body.get("ops")
            if not isinstance(ops, list) or not ops:
                return self._json({"ok": False, "msg": "没有要执行的操作"})
            # 重名策略：skip 跳过（默认）/ overwrite 覆盖，其余值一律按 skip
            ondup = str(body.get("ondup") or "skip").lower()
            if ondup not in ("skip", "overwrite"):
                ondup = "skip"
            good, errs = pan_tools.validate_plan({"ops": ops}, sandbox)
            if not good:
                return self._json({"ok": False, "msg": "计划校验不通过：" + "；".join(errs[:3])})
            # 安全上限：单次最多 2000 条，避免误操作一次搬空整个网盘
            if len(good) > 2000:
                return self._json({"ok": False,
                                   "msg": f"一次最多执行 2000 条，当前 {len(good)} 条，请缩小范围"})
            # 执行可能几百条、要好几分钟，进度交给前端轮询 /api/pan_apply_progress
            exec_begin(len(good))
            try:
                stat = pan_tools.apply_plan(pan, good, ondup=ondup,
                                            on_progress=exec_tick)
            except Exception as e:
                return self._json({"ok": False, "msg": f"执行失败：{e}"})
            finally:
                exec_end()
            # 整批都是删空文件夹时，报「删除 12 个空文件夹」比「改名 0 / 移动 0 / 删除 12」
            # 直观得多——那些 0 对用户毫无信息量
            dir_del = sum(1 for o in good if o["op"] == "delete" and o.get("isdir"))
            if dir_del and dir_del == stat["delete"]:
                msg = f"完成：删除 {stat['delete']} 个空文件夹，失败 {len(stat['fails'])}"
            else:
                msg = (f"完成：改名 {stat['rename']} / 移动 {stat['move']} / "
                       f"删除 {stat['delete']}，失败 {len(stat['fails'])}")
            if stat.get("backup"):
                msg += (f"；覆盖旧文件 {stat['backup']} 个"
                        f"（已备份到 {stat['backup_dir']}）")
            if stat["fails"]:
                # 实测：百度对「刚变动过」的文件会回 -9「文件不存在」的假失败，
                # 而操作其实生效了（130 条全报 -9，刷新目录一看名字全改好了）。
                # 不提示的话用户会以为白干一场。
                msg += "；注：百度对刚变动的文件常假报失败，刷新目录核对一下，往往已经生效"
            # 执行过的预览文件盖个戳：列表里显示「已执行」，而且不再被复用——
            # 网盘已经不是那份预览生成时的样子，再拿它当「当前状态」就是错的了
            preview_id = str(body.get("preview_id") or "")
            if preview_id:
                preview_store.mark_executed(PROJ, preview_id, stat, msg)
            return self._json({"ok": True, "stat": stat, "errors": errs, "msg": msg,
                               "preview_id": preview_id})

        # 导出文件清单（给 AI 用）
        if path == "/api/pan_export":
            p = str(body.get("path") or sandbox).strip() or sandbox
            recursive = bool(body.get("recursive", True))
            limit = int(body.get("limit", 300) or 300)
            scan_begin()
            cancelled = False
            failures = []       # 同上：清单不完整得让 AI 和用户都知道
            try:
                files = pan.list_files(p, recursive=recursive, on_progress=scan_tick,
                                       should_stop=scan_should_stop, failures=failures)
            except pan_tools.ScanCancelled as e:
                cancelled = True
                return self._json({"ok": False, "cancelled": True, "msg":
                                   f"已停止扫描（已遍历 {e.dirs} 个目录 / "
                                   f"找到 {e.files} 个文件），清单未生成"})
            except Exception as e:
                m = str(e) if isinstance(e, pan_tools.PanError) else f"扫描失败：{e}"
                return self._json({"ok": False, "msg": m})
            finally:
                scan_end(cancelled=cancelled)
            files = files[:limit]
            lines = [f'{i+1}\t{f["path"]}\t{f["size"]}' for i, f in enumerate(files)]
            # 清单不完整时把这件事写进注释：AI 拿到的是残缺清单，得让它知道
            # 有些目录根本没扫进去，别把「清单里没有」当成「网盘里没有」
            skip_note = ""
            if failures:
                sample = "；".join(f["path"].rsplit("/", 1)[-1] for f in failures[:3])
                skip_note = (f"# 注意：有 {len(failures)} 个目录列不开（百度侧脏条目），"
                             f"它们下面的文件不在清单里，例如：{sample}\n")
            hint = (
                f"# 以下是百度网盘沙盒目录 {p} 下的 {len(files)} 个文件（路径\t大小字节）。\n"
                + skip_note
                + f"# 请按要求生成操作计划，输出严格的 JSON，格式：\n"
                f'# {{"ops":[{{"op":"rename","path":"...","newname":"..."}},'
                f'{{"op":"move","path":"...","dest":"..."}},'
                f'{{"op":"delete","path":"..."}}]}}\n'
                f"# 约束：1) path 必须是上面列出的完整路径，不要自造；"
                f"2) 只允许操作 {sandbox} 内的文件；3) 不要输出解释文字，只输出 JSON。\n")
            return self._json({"ok": True, "text": hint + "\n".join(lines),
                               "count": len(files), "total": len(files),
                               "skipped_total": len(failures)})

        # 校验粘贴进来的 AI 计划
        if path == "/api/pan_validate":
            txt = str(body.get("text") or "").strip()
            if not txt:
                return self._json({"ok": False, "msg": "请粘贴计划 JSON"})
            try:
                plan = json.loads(txt)
            except Exception as e:
                return self._json({"ok": False, "msg": f"JSON 解析失败：{e}"})
            good, errs = pan_tools.validate_plan(plan, sandbox)
            return self._json({"ok": bool(good), "ops": good[:2000],
                               "total": len(good), "errors": errs,
                               "msg": (f"解析出 {len(good)} 条合法操作"
                                       + (f"，{len(errs)} 条被拦截" if errs else ""))})

        return self._json({"ok": False, "msg": f"接口不存在：{path}"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        self.handle_api(self.path.split("?")[0], body)


def main():
    # 自检：页面文件存在
    if not HTMLFILE.exists():
        print(f"[错误] 找不到 {HTMLFILE.name}")
        sys.exit(1)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"[提示] 端口 {PORT} 已被占用 —— 控制面板多半已经在运行了。")
        print(f"       请直接浏览器打开：http://127.0.0.1:{PORT}/")
        input("按回车键退出…")
        sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"控制面板已启动：{url}  （Ctrl+C 退出，不影响正在进行的上传）")
    if not os.environ.get("WEBUI_NO_BROWSER"):   # 测试模式下不弹浏览器
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n控制面板已关闭（若上传仍在进行，它会继续跑完）")


if __name__ == "__main__":
    main()
