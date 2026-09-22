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

PROJ = Path(__file__).resolve().parent
PY = sys.executable                      # 用当前解释器跑上传子进程
CONFIG = PROJ / "config.json"
TOKEN = PROJ / "token.json"
LOGFILE = PROJ / "upload.log"
PROGRESS = PROJ / "progress.json"
HTMLFILE = PROJ / "webui.html"
PIDFILE = PROJ / "upload.pid"            # 上传进程锁：面板重启也能认出还在跑的上传
PORT = 8765
LOG_MAX_BYTES = 20 * 1024 * 1024         # 日志超过 20MB 就归档，避免长期任务撑爆磁盘

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
_scan = {"running": False, "phase": "", "dirs": 0, "files": 0, "path": "", "t0": 0.0}


def scan_begin():
    with _scan_lock:
        _scan.update(running=True, phase="扫描目录", dirs=0, files=0,
                     path="", t0=time.time())


def scan_tick(dirs, files, cur):
    """list_dir 每列完一个目录回调一次"""
    with _scan_lock:
        _scan.update(dirs=dirs, files=files, path=cur)


def scan_phase(phase):
    with _scan_lock:
        _scan["phase"] = phase


def scan_end():
    with _scan_lock:
        _scan["running"] = False


def scan_snapshot():
    with _scan_lock:
        snap = dict(_scan)
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
                self._json({"ok": True, "config": cfg})
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

        return self._json({"ok": False, "msg": f"接口不存在：{path}"}, 404)

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
                return self._json({"ok": False, "msg": f"列目录失败：{e}"})
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
            # 递归扫描可能是几千个目录的活儿，把进度摊给前端轮询
            scan_begin()
            try:
                files = pan.list_files(p, recursive=recursive, on_progress=scan_tick)
            except Exception as e:
                return self._json({"ok": False, "msg": f"扫描失败：{e}"})
            finally:
                scan_end()
            scan_phase("生成计划")
            if kind == "rename":
                ops, unchanged = pan_tools.build_rename_plan(
                    files, str(body.get("mode") or "replace"), body.get("params") or {})
                return self._json({"ok": True, "ops": ops[:2000], "total": len(ops),
                                   "unchanged": len(unchanged), "scanned": len(files)})
            if kind == "organize":
                dest = str(body.get("dest") or "").strip()
                if not dest:
                    return self._json({"ok": False, "msg": "请填写归档目标目录"})
                ops = pan_tools.build_organize_plan(
                    files, str(body.get("by") or "category"), dest, sandbox)
                return self._json({"ok": True, "ops": ops[:2000], "total": len(ops),
                                   "scanned": len(files)})
            if kind == "delete":
                ops = pan_tools.build_delete_plan(files, body.get("filters") or {})
                return self._json({"ok": True, "ops": ops[:2000], "total": len(ops),
                                   "scanned": len(files)})
            return self._json({"ok": False, "msg": f"未知的计划类型：{kind}"})

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
            try:
                stat = pan_tools.apply_plan(pan, good, ondup=ondup)
            except Exception as e:
                return self._json({"ok": False, "msg": f"执行失败：{e}"})
            return self._json({"ok": True, "stat": stat, "errors": errs,
                               "msg": (f"完成：改名 {stat['rename']} / 移动 {stat['move']} / "
                                       f"删除 {stat['delete']}，失败 {len(stat['fails'])}"
                                       + (f"；覆盖旧文件 {stat['backup']} 个（已备份到 {stat['backup_dir']}）"
                                          if stat.get("backup") else ""))})

        # 导出文件清单（给 AI 用）
        if path == "/api/pan_export":
            p = str(body.get("path") or sandbox).strip() or sandbox
            recursive = bool(body.get("recursive", True))
            limit = int(body.get("limit", 300) or 300)
            scan_begin()
            try:
                files = pan.list_files(p, recursive=recursive, on_progress=scan_tick)
            except Exception as e:
                return self._json({"ok": False, "msg": f"扫描失败：{e}"})
            finally:
                scan_end()
            files = files[:limit]
            lines = [f'{i+1}\t{f["path"]}\t{f["size"]}' for i, f in enumerate(files)]
            hint = (
                f"# 以下是百度网盘沙盒目录 {p} 下的 {len(files)} 个文件（路径\t大小字节）。\n"
                f"# 请按要求生成操作计划，输出严格的 JSON，格式：\n"
                f'# {{"ops":[{{"op":"rename","path":"...","newname":"..."}},'
                f'{{"op":"move","path":"...","dest":"..."}},'
                f'{{"op":"delete","path":"..."}}]}}\n'
                f"# 约束：1) path 必须是上面列出的完整路径，不要自造；"
                f"2) 只允许操作 {sandbox} 内的文件；3) 不要输出解释文字，只输出 JSON。\n")
            return self._json({"ok": True, "text": hint + "\n".join(lines),
                               "count": len(files), "total": len(files)})

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
