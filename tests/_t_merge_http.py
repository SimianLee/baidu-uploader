# -*- coding: utf-8 -*-
"""HTTP 级：合并重复三层都走得通（以前只有目录层那半边）

界面上「合并重复」只有 txt(1)→txt 一种算法时，同在一层里的 `书(1).epub`
`书(2).epub` 一条都扫不出来——用户看到的「功能没加上」就是这一半。这里在
真服务 + 假网盘上把三种 target 各跑一遍： kind/target 路由对不对、两层同做
时会不会互相打架、文件层的计划能不能真的执行掉。
"""
import json
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import webui  # noqa: E402
import preview_store as ps  # noqa: E402

PORT = 8797
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


class FakePan:
    """网盘里同时摆着：重复目录一对 + 同层的重复文件一组 + 大小不同的冒牌货"""

    sandbox = "/apps/root"

    def __init__(self):
        self.dirs = [self.sandbox, f"{self.sandbox}/归档",
                     f"{self.sandbox}/归档/txt", f"{self.sandbox}/归档/txt(1)"]
        self.files = [
            (f"{self.sandbox}/归档/txt/书.epub", 100),
            (f"{self.sandbox}/归档/txt(1)/书.epub", 100),      # 目录层重复的那份
            (f"{self.sandbox}/归档/讲义.pdf", 100),
            (f"{self.sandbox}/归档/讲义(1).pdf", 100),         # 文件层重复
            (f"{self.sandbox}/归档/讲义(2).pdf", 100),
            (f"{self.sandbox}/归档/报告(1).pdf", 4096),        # 大小不同：成套资料
            (f"{self.sandbox}/归档/报告(2).pdf", 8192),
        ]
        self.calls = []

    @staticmethod
    def _rec(path, isdir=False, size=0):
        return {"path": path, "name": path.rsplit("/", 1)[-1], "size": size,
                "isdir": isdir, "mtime": 1758600000}

    def _tree(self):
        return ([self._rec(d, True) for d in self.dirs]
                + [self._rec(p, False, s) for p, s in self.files])

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        p = path.rstrip("/")
        out = [e for e in self._tree()
               if e["path"] != p and e["path"].startswith(p + "/")
               and (recursive or "/" not in e["path"][len(p) + 1:])]
        if on_progress:
            on_progress(1, sum(1 for x in out if not x["isdir"]), p)
        return out

    def list_files(self, path, recursive=True, on_progress=None,
                   should_stop=None, failures=None):
        return [e for e in self.list_dir(path, recursive=recursive,
                                         on_progress=on_progress,
                                         failures=failures) if not e["isdir"]]

    def mkdir(self, path):
        return True

    def move_batch(self, ops, ondup="skip", on_progress=None):
        self.calls.append(("move", len(ops)))
        if on_progress:
            on_progress(len(ops), 0)
        return len(ops), []

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        self.calls.append(("rename", len(ops)))
        if on_progress:
            on_progress(len(ops), 0)
        return len(ops), []

    def delete_batch(self, paths, on_progress=None):
        self.calls.append(("delete", len(paths)))
        if on_progress:
            on_progress(len(paths), 0)
        return len(paths), []


PAN = FakePan()


def paths_of(d):
    return sorted(o["path"] for o in d.get("ops") or [])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mergehttp_"))
    try:
        saved = (webui.PROJ, webui.CONFIG, webui.PROGRESS, webui.PIDFILE,
                 webui.LOGFILE)
        webui.PROJ = tmp
        webui.CONFIG = tmp / "config.json"
        webui.PROGRESS = tmp / "progress.json"
        webui.PIDFILE = tmp / "upload.pid"
        webui.LOGFILE = tmp / "upload.log"
        webui.CONFIG.write_text(json.dumps(
            {"app_key": "k", "secret_key": "s", "local_dir": str(tmp),
             "remote_dir": PAN.sandbox}), encoding="utf-8")
        webui.Handler._pan = lambda self: (PAN, PAN.sandbox)
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        try:
            print("【1】target=both：目录层 + 文件层一起出结果")
            d = api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                      "kind": "merge_dirs", "target": "both",
                                      "how": "dedupe", "ondup": "skip"})
            ck(d["ok"], "生成预览成功", d.get("msg"))
            ck(d.get("pairs") == 1, "目录层：1 对重复目录", d.get("pairs"))
            ck(d.get("fgroups") == 1, "★ 文件层：1 组重复文件", d.get("fgroups"))
            ck(d.get("fdup_deleted") == 2, "★ 文件层删掉 2 个多余副本",
               d.get("fdup_deleted"))
            ck(d.get("fsize_diff") == 1,
               "★ 大小不同的那一个当成成套资料放过（本身另一册）",
               d.get("fsize_diff"))
            ck(d.get("frenamed") == 0,
               "★ 也没给没证据的猜测正名（报告(1) 不许改成 报告）",
               d.get("frenamed"))
            ps_ = paths_of(d)
            ck(len(ps_) == len(set(ps_)), "★ 没有哪条路径被安排两次", ps_)
            ck(f"{PAN.sandbox}/归档/讲义(1).pdf" in ps_, "讲义(1) 进了清单", ps_)
            ck(f"{PAN.sandbox}/归档/报告(1).pdf" not in ps_,
               "★ 大小不同的冒牌货没被算进去", ps_)
            # label 不跟着响应回客户端，存在预览记录里（下拉框显示那份）
            lab = (ps.load_preview(tmp, d["preview_id"]) or {}).get("label") or ""
            ck("目录" in lab and "文件" in lab, "标题写明两层都做了", lab)

            print("\n【2】target=file：只处理文件层，目录副本不动")
            d2 = api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                       "kind": "merge_dirs", "target": "file",
                                       "how": "dedupe", "ondup": "skip"})
            ck(d2["ok"], "生成预览成功", d2.get("msg"))
            ck(d2.get("pairs") is None, "不带目录层统计", d2.get("pairs"))
            ps2 = paths_of(d2)
            ck(len(ps2) == 2 and all("讲义" in p for p in ps2),
               "★ 清单里只有讲义那组的两个副本", ps2)
            ck(d2.get("frenamed") == 0, "没有需要正名的（主名都在）", d2.get("frenamed"))
            lab2 = (ps.load_preview(tmp, d2["preview_id"]) or {}).get("label") or ""
            ck(lab2.startswith("合并重复文件"), "标题写明只是文件层", lab2)

            print("\n【3】target=dir：回到老路子，同层副本不动")
            d3 = api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                       "kind": "merge_dirs", "target": "dir",
                                       "how": "dedupe", "ondup": "skip"})
            ck(d3["ok"], "生成预览成功", d3.get("msg"))
            ck(d3.get("fgroups") is None, "不带文件层统计", d3.get("fgroups"))
            ck(paths_of(d3) == [f"{PAN.sandbox}/归档/txt(1)/书.epub"],
               "★ 只有目录层那一对里的重复文件", paths_of(d3))

            print("\n【4】脏 target 不许跳过任何一层")
            d4 = api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                       "kind": "merge_dirs", "target": "乱码?",
                                       "how": "dedupe", "ondup": "skip"})
            ck(paths_of(d4) == ps_, "★ 认不出来的 target 按 both 全做", paths_of(d4))

            print("\n【5】改了 target 就得重算，不许复用旧预览")
            again = api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                          "kind": "merge_dirs", "target": "both",
                                          "how": "dedupe", "ondup": "skip"})
            ck(again.get("reused") is True, "同参数第二次复用缓存", again.get("reused"))
            ck(again["preview_id"] != d2["preview_id"],
               "★ target 不同 ⇒ 是两份各自的预览", (again.get("preview_id"),
                                                d2.get("preview_id")))

            print("\n【6】真的执行掉：删的删、改的改")
            PAN.calls.clear()
            r = api("/api/pan_apply", {"ops": d["ops"], "ondup": "skip",
                                       "preview_id": d["preview_id"],
                                       "total": d["total"],
                                       "backup_delete": False})
            ck(r["ok"], "执行成功", r.get("msg"))
            st = r.get("stat") or {}
            ck(st.get("delete", 0) >= 2, "★ 删掉了重复的副本",
               {k: v for k, v in st.items() if k in ("delete", "rename", "move")})
            ck(("delete", 2) in [(k, n) for k, n in PAN.calls] or
               any(k == "delete" and n >= 2 for k, n in PAN.calls),
               "★ 假网盘确实收到了删除", PAN.calls)
        finally:
            srv.shutdown()
            for attr, val in zip(("PROJ", "CONFIG", "PROGRESS", "PIDFILE",
                                  "LOGFILE"), saved):
                setattr(webui, attr, val)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
