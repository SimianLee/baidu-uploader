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
PORT = 8765

# ---------------- 上传子进程管理 ----------------
_state = {"proc": None, "lock": threading.Lock()}


def _base_env():
    return {**os.environ, "PYTHONPATH": str(PROJ / "libs")}


def start_upload(limit: int = 0):
    with _state["lock"]:
        p = _state["proc"]
        if p is not None and p.poll() is None:
            return False, "已有上传任务在运行中"
        cmd = [PY, "-X", "utf8", "-u", str(PROJ / "upload_baidu.py")]
        if limit and limit > 0:
            cmd += ["--limit", str(limit)]
        cmd += ["--yes"]   # 无交互环境，跳过末尾手动确认（after_upload 用 move/trash/keep）
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
        return False, "当前没有运行中的上传任务"


def is_running():
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
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    return ("https://openapi.baidu.com/oauth/2.0/authorize?"
            f"response_type=code&client_id={cfg['app_key']}"
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


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a):   # 静默访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/webui.html"):
            body = HTMLFILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/config":
            try:
                cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig"))
                self._json({"ok": True, "config": cfg})
            except Exception as e:
                self._json({"ok": False, "msg": f"读取配置失败：{e}"})
        elif self.path == "/api/progress":
            prog = {}
            if PROGRESS.exists():
                try:
                    prog = json.loads(PROGRESS.read_text(encoding="utf-8-sig"))
                except Exception:
                    pass
            self._json({"ok": True, "running": is_running(),
                        "progress": prog, "log": tail_text(LOGFILE)})
        elif self.path == "/api/authurl":
            self._json({"ok": True, "url": auth_url(), "has_token": TOKEN.exists()})
        else:
            self._json({"ok": False, "msg": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}

        if self.path == "/api/config":
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._json({"ok": False, "msg": "配置格式错误"})
            # 基本字段校验
            for k in ("app_key", "secret_key", "local_dir", "remote_dir"):
                if not str(cfg.get(k, "")).strip():
                    return self._json({"ok": False, "msg": f"缺少必填项：{k}"})
            for k in ("batch_size", "batch_pause_sec", "file_interval_sec", "max_file_size_mb"):
                try:
                    cfg[k] = int(cfg[k])
                except Exception:
                    return self._json({"ok": False, "msg": f"{k} 必须是数字"})
            cfg["recursive"] = bool(cfg.get("recursive", True))
            cfg["chunk_size_mb"] = 4
            # after_upload 只允许三种（网页端无交互，不支持 ask）
            if cfg.get("after_upload") not in ("keep", "move", "trash"):
                cfg["after_upload"] = "keep"
            if not str(cfg.get("done_dir", "")).strip():
                cfg["done_dir"] = str(Path(cfg["local_dir"]).parent / "已上传")
            CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            return self._json({"ok": True, "msg": "配置已保存"})

        if self.path == "/api/start":
            limit = int(body.get("limit", 0) or 0)
            ok, msg = start_upload(limit)
            return self._json({"ok": ok, "msg": msg})

        if self.path == "/api/stop":
            ok, msg = stop_upload()
            return self._json({"ok": ok, "msg": msg})

        if self.path == "/api/dryrun":
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

        if self.path == "/api/login":
            code = str(body.get("code", "")).strip()
            if not code:
                return self._json({"ok": False, "msg": "请填写授权码"})
            ok, msg = do_login(code)
            return self._json({"ok": ok, "msg": msg})

        if self.path == "/api/verify":
            ok, msg = verify_token()
            return self._json({"ok": ok, "msg": msg})

        return self._json({"ok": False, "msg": "not found"}, 404)


def main():
    # 自检：页面文件存在
    if not HTMLFILE.exists():
        print(f"[错误] 找不到 {HTMLFILE.name}")
        sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
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
