# -*- coding: utf-8 -*-
r"""本地整理走完整 HTTP 链路：从页面发请求到磁盘上真的改了名/搬了家

离线那份（_t_local_fs）验的是 LocalFiles 本身；这里验的是**接缝**：
前端把 src=local 一路带到后端后，浏览、生成预览、存预览、载入预览、执行
这五步是不是都对本地路径成立。重点守住三件容易悄悄坏掉的事：

1. 预览存下来再载入时，越界校验不能用网盘沙盒去比本地路径（会整批被拦）；
2. src 必须进指纹——不然从本地切回网盘会拿另一边的旧清单顶上；
3. 越界的本地目录必须被挡在门外（整理功能绝不能碰到根以外的东西）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import local_fs as lf  # noqa: E402
import preview_store as ps  # noqa: E402
import webui  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra!r}")


def api(path, body=None, method=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"HTTP 失败 {e}"}


def touch(p, size=10):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


# ------------------------------------------------------------------ 假网盘
class FakePan:
    """src=remote 时的替身：只为验「换个 src 就是另一份预览」"""

    sandbox = "/apps/localtest"

    def __init__(self):
        self.tree = [{"path": f"{self.sandbox}/a.txt", "name": "a.txt",
                      "isdir": False, "size": 1, "mtime": 1}]

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        return list(self.tree)

    def list_files(self, path, recursive=True, on_progress=None,
                   should_stop=None, failures=None):
        return list(self.tree)

    def list_names(self, path):
        return {"a.txt"}

    def mkdir(self, path):
        return True

    def check_path(self, p):
        return str(p)

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        return len(ops), []

    def move_batch(self, ops, ondup="skip", on_progress=None):
        return len(ops), []

    def delete_batch(self, paths, on_progress=None):
        return len(paths), []


PAN = FakePan()

tmp = Path(tempfile.mkdtemp(prefix="localhttp_"))
root = tmp / "书籍"
for rel in ("小说/a.txt", "小说/b.pdf", "图片/c.png"):
    touch(root / rel, 100)
(root / "空目录").mkdir(parents=True, exist_ok=True)


def fake_pan(self, src="remote", root_=None):
    if str(src).lower() == "local":
        p = lf.LocalFiles(root_ or str(root))
        return p, p.sandbox
    return PAN, PAN.sandbox


def main():
    global PASS, FAIL
    try:
        saved = (webui.PROJ, webui.CONFIG, webui.PROGRESS, webui.PIDFILE,
                 webui.LOGFILE)
        webui.PROJ = tmp
        webui.CONFIG = tmp / "config.json"
        webui.PROGRESS = tmp / "progress.json"
        webui.PIDFILE = tmp / "upload.pid"
        webui.LOGFILE = tmp / "upload.log"
        webui.CONFIG.write_text(json.dumps(
            {"app_key": "k", "secret_key": "s", "local_dir": str(root),
             "remote_dir": PAN.sandbox}), encoding="utf-8")
        webui.Handler._pan = fake_pan
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        print("【1】浏览本地目录：先给盘符，再给目录里的内容")
        d = api("/api/pan_browse", {"src": "local", "path": ""})
        ck(d["ok"] and d.get("drives") is True, "★ 不填路径 → 返回盘符列表",
           d.get("msg"))
        ck(any(x["path"].endswith(":/") for x in d.get("dirs") or []),
           "盘符形如 C:/", [x["path"] for x in d.get("dirs") or []][:3])
        d = api("/api/pan_browse", {"src": "local", "path": str(root)})
        ck(d["ok"] and d.get("sandbox") == str(root).replace("\\", "/"),
           "列本地目录成功，根目录回传正确", d.get("sandbox"))
        ck(sorted(x["name"] for x in d["dirs"]) == ["图片", "小说", "空目录"],
           "★ 子目录列出来了", sorted(x["name"] for x in d["dirs"]))
        ck(d.get("file_total") == 0, "这一层没有文件（都在子目录里）",
           d.get("file_total"))
        d2 = api("/api/pan_browse", {"src": "local", "path": str(root / "小说")})
        ck(d2.get("file_total") == 2, "进子目录能看到 2 个文件",
           d2.get("file_total"))

        print("\n【2】越界的本地目录一律挡在门外")
        outside = str(tmp)
        d = api("/api/pan_plan", {"src": "local", "path": outside,
                                  "kind": "rename", "mode": "replace",
                                  "params": {"find": "a", "replace": "A"}})
        ck(d["ok"] is False and "不在本地根" in (d.get("msg") or ""),
           "★ 根目录以外不给生成计划", d.get("msg"))

        print("\n【3】改名：预览 → 载入 → 执行，磁盘上真的改了")
        d = api("/api/pan_plan", {"src": "local", "path": str(root),
                                  "recursive": True, "kind": "rename",
                                  "mode": "replace",
                                  "params": {"find": "a", "replace": "A"}})
        ck(d["ok"], "生成改名预览成功", d.get("msg"))
        ck(d.get("total") == 1, "★ 只挑中 a.txt 一个（b/c 不含 a）", d.get("total"))
        rec = ps.load_preview(tmp, d["preview_id"]) or {}
        ck((rec.get("payload") or {}).get("src") == "local",
           "★ src=local 记进了预览（切回网盘不会复用它）",
           (rec.get("payload") or {}).get("src"))
        ck(rec.get("sandbox") == str(root).replace("\\", "/"),
           "预览里存的是本地根", rec.get("sandbox"))
        ld = api("/api/preview_load", {"id": d["preview_id"]})
        ck(ld["ok"] and "已载入" in (ld.get("msg") or ""),
           "★ 本地预览能正常载入（没被当成越界）", ld.get("msg"))
        ck(not ld.get("errors"), "载入时没有越界告警", ld.get("errors"))
        r = api("/api/pan_apply", {"src": "local", "ops": d["ops"],
                                   "preview_id": d["preview_id"],
                                   "total": d["total"], "ondup": "skip"})
        ck(r["ok"], "执行成功", r.get("msg"))
        # NTFS 大小写不敏感，a.txt 的 Path.exists() 对 A.txt 也返回真——
        # 只能按目录里真实的名字判定
        names = os.listdir(root / "小说")
        ck("A.txt" in names and "a.txt" not in names,
           "★ 磁盘上真的改名了", names)

        print("\n【4】整理归档 + 删空目录：本地同样成立")
        d = api("/api/pan_plan", {"src": "local", "path": str(root),
                                  "recursive": True, "kind": "organize",
                                  "by": "ext", "dest": str(root / "归档")})
        ck(d["ok"] and d.get("total") == 3, "归档计划 3 条", d.get("total"))
        r = api("/api/pan_apply", {"src": "local", "ops": d["ops"],
                                   "preview_id": d["preview_id"],
                                   "total": d["total"], "ondup": "skip"})
        ck(r["ok"] and (r.get("stat") or {}).get("move") == 3,
           "★ 3 个文件真的搬进了归档目录", (r.get("stat") or {}).get("move"))
        ck((root / "归档" / "txt").is_dir() and (root / "归档" / "pdf").is_dir(),
           "★ 按后缀分好的目录建在磁盘上了", sorted(os.listdir(root / "归档")))
        d = api("/api/pan_plan", {"src": "local", "path": str(root),
                                  "kind": "empty_dir"})
        ck(d["ok"] and d.get("total") >= 1, "★ 空目录被扫出来", d.get("total"))
        r = api("/api/pan_apply", {"src": "local", "ops": d["ops"],
                                   "preview_id": d["preview_id"],
                                   "total": d["total"], "ondup": "skip"})
        ck(r["ok"] and not (root / "空目录").exists(),
           "★ 空目录真的删掉了", r.get("msg"))

        print("\n【5】整理本地时上传还在跑 → 直接拒绝（两边会打架）")
        webui.is_running = lambda: True
        try:
            r = api("/api/pan_apply", {"src": "local",
                                       "ops": [{"op": "delete",
                                                "path": str(root / "归档" / "txt" / "A.txt")}],
                                       "total": 1, "ondup": "skip"})
            ck(r["ok"] is False and "上传任务正在运行" in (r.get("msg") or ""),
               "★ 上传中不给整理本地", r.get("msg"))
        finally:
            webui.is_running = saved_running

        print("\n【6】★ 归档目录在整理范围之外：生成时就拦，且说清该改哪个框")
        sub = root / "子目录"
        touch(sub / "z.txt", 100)
        d = api("/api/pan_plan", {"src": "local", "path": str(sub),
                                  "recursive": True, "kind": "organize", "by": "ext",
                                  "dest": str(root / "已整理"), "root": str(sub)})
        msg = d.get("msg") or ""
        ck(d["ok"] is False, "★ 越界的归档目录不给生成计划", msg)
        ck("整理范围之外" in msg, "★ 说清是「归档目录在整理范围之外」", msg)
        ck(str(sub).replace("\\", "/") in msg, "★ 报出整理范围是哪个目录", msg)
        ck("已整理" in msg, "报出归档目录是哪个", msg)
        ck(str(root).replace("\\", "/") in msg,
           "★ 给出「把操作目录改成两者的公共上级」这条明路", msg)
        # 反例：整理范围定在上一级（浏览进子目录不该把范围收窄）→ 归档合法
        d = api("/api/pan_plan", {"src": "local", "path": str(sub),
                                  "recursive": True, "kind": "organize", "by": "ext",
                                  "dest": str(root / "已整理"), "root": str(root)})
        ck(d["ok"] and d.get("total") == 1,
           "★ 整理范围定在上一级 → 归档到上一级就合法了", d.get("msg"))

        print("\n【7】★ 清理空文件夹：本地套娃必须一层层删干净")
        rs = str(root).replace("\\", "/")
        for d in ("套娃/A/B/C", "套娃/A/D"):
            (root / d).mkdir(parents=True, exist_ok=True)
        d = api("/api/pan_plan", {"src": "local", "path": str(root),
                                  "kind": "empty_dir", "root": str(root)})
        ck(d["ok"], "生成空文件夹计划成功", d.get("msg"))
        rel = [str(o["path"]).replace(rs, "") for o in d.get("ops") or []]
        ck(any(x.endswith("/套娃/A/B/C") for x in rel),
           "★ 最深的那一层也在清单里（不是只有外层一条）", rel)
        ck(sum(1 for x in rel if x.startswith("/套娃")) >= 4,
           "★ 套娃的每一层都列出来了", rel)
        r = api("/api/pan_apply", {"src": "local", "ops": d["ops"],
                                   "preview_id": d["preview_id"],
                                   "total": d["total"], "ondup": "skip"})
        ck(r["ok"] and not (r.get("stat") or {}).get("fails"),
           "★ 执行没有失败项", (r.get("stat") or {}).get("fails"))
        ck(not (root / "套娃").exists(), "★ 套娃整棵树从磁盘上消失了", None)
        ck((root / "归档" / "txt").exists(), "★ 存着文件的归档目录一个没动", None)

        print("\n【8】src 不同 = 另一份预览，不许互相顶替")
        f_loc = ps.fingerprint("rename", {"path": "X:/x", "src": "local"}, "X:/x")
        f_rem = ps.fingerprint("rename", {"path": "X:/x", "src": "remote"}, "X:/x")
        ck(f_loc != f_rem, "★ 同一条路径，local 与 remote 指纹不同",
           (f_loc[:12], f_rem[:12]))

        webui.PROJ, webui.CONFIG, webui.PROGRESS = saved[0], saved[1], saved[2]
        webui.PIDFILE, webui.LOGFILE = saved[3], saved[4]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


saved_running = webui.is_running
try:
    main()
except Exception:
    import traceback
    traceback.print_exc()
    FAIL += 1

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
