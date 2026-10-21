# -*- coding: utf-8 -*-
r"""导出文件清单 / 按表格操作（改名 + 删除）

两件事是一条链上的：

1. `/api/pan_list_export` 把目录里的文件列成 CSV（文件名 / 后缀 / 路径 / 大小 / 时间），
   关键的是表里**预留了「操作」和「新文件名」两个空列**——导出的表就是导入的坯子，
   不用手抄几千行路径；
2. `/api/pan_plan` 的 `kind=sheet_ops` 把这张表翻译成计划：写「改名」的改名，
   写「删除」的真删，混在一张表里也行。

这套用例守的是：
  · 表格形态认得全（中文/英文表头、列序打乱、没表头、只有删除专用两列）
  · 向后兼容：老的两列表格（路径 + 新名）照样是整张按改名算
  · 危险一律拦在生成预览那一步：认不出的操作、改名没给新名、名字带斜杠、路径出界
  · 端到端：导出的表回填两列再导回来，执行后**网盘上真的改了名、真的删了**
"""
import csv
import io
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

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
SB = "/apps/x"
PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra!r}")


def api(path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "msg": f"HTTP 失败 {e}"}


def rows_of(text):
    return [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(c.strip() for c in r)]


class FakePan:
    """会变的假网盘：改名真的改 tree，删除真的删掉条目"""

    sandbox = SB

    def __init__(self, paths):
        self.tree = [{"path": p, "name": p.rsplit("/", 1)[-1], "isdir": False,
                      "size": 100, "mtime": 1700000000} for p in paths]

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        return list(self.tree)

    def list_files(self, path, recursive=True, on_progress=None,
                   should_stop=None, failures=None):
        return [f for f in self.tree if not f["isdir"]]

    def list_names(self, path):
        return {f["name"] for f in self.tree}

    def mkdir(self, path):
        return True

    def check_path(self, p):
        return str(p)

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        ok, fails = 0, []
        for o in ops:
            for f in self.tree:
                if f["path"] == o["path"]:
                    f["path"] = f["path"].rsplit("/", 1)[0] + "/" + o["newname"]
                    f["name"] = o["newname"]
                    ok += 1
                    break
            else:
                fails.append({"path": o["path"], "msg": "找不到这个文件"})
        return ok, fails

    def move_batch(self, ops, ondup="skip", on_progress=None):
        return len(ops), []

    def delete_batch(self, paths, on_progress=None):
        before = len(self.tree)
        gone = set(paths)
        self.tree[:] = [f for f in self.tree if f["path"] not in gone]
        return before - len(self.tree), []


H = webui.Handler


def main():
    global PASS, FAIL
    tmp = Path(tempfile.mkdtemp(prefix="sheetcsv_"))
    try:
        saved = (webui.PROJ, webui.CONFIG, webui.PROGRESS, webui.PIDFILE,
                 webui.LOGFILE)
        webui.PROJ = tmp
        webui.CONFIG = tmp / "config.json"
        webui.PROGRESS = tmp / "progress.json"
        webui.PIDFILE = tmp / "upload.pid"
        webui.LOGFILE = tmp / "upload.log"
        webui.CONFIG.write_text(json.dumps(
            {"app_key": "k", "secret_key": "s", "remote_dir": SB,
             "local_dir": str(tmp)}), encoding="utf-8")

        pan = FakePan([f"{SB}/书/a.txt", f"{SB}/书/b.txt",
                       f"{SB}/书/c.txt", f"{SB}/书/d.pdf"])
        webui.Handler._pan = lambda self, src="remote", root=None: (pan, pan.sandbox)
        ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        print("【1】★ 导出文件清单：文件名和后缀都在表里")
        d = api("/api/pan_list_export", {"path": SB, "recursive": True})
        ck(d["ok"] and d.get("count") == 4, "导出了 4 个文件", d.get("msg"))
        rs = rows_of(d["text"])
        head = rs[0]
        ck(head == webui.Handler.LIST_COLS, "★ 表头是约定的那 9 列", head)
        ck("后缀" in head and "文件名" in head, "★ 有「文件名」和「后缀」两列", head)
        body = {r[2]: r for r in rs[1:]}
        ck(body[f"{SB}/书/a.txt"][3] == "a.txt", "文件名对了", body[f"{SB}/书/a.txt"][:6])
        ck(body[f"{SB}/书/a.txt"][4] == "txt", "★ 后缀对了（不带点）",
           body[f"{SB}/书/a.txt"][:6])
        ck(body[f"{SB}/书/d.pdf"][4] == "pdf", "★ 不同后缀分别认出来", None)
        ck(body[f"{SB}/书/a.txt"][6] == f"{SB}/书", "所在目录单独一列", None)
        ck(bool(d.get("filename")), "带了一个能直接存的文件名", d.get("filename"))

        print("\n【2】★ 导出的表就是导入的坯子：填两列再导回来就能执行")
        rs = rows_of(d["text"])
        hdr = rs[0]
        i_op, i_path, i_new = hdr.index("操作"), hdr.index("原路径"), hdr.index("新文件名")
        rs[1][i_op], rs[1][i_new] = "改名", "三体.txt"        # 第一行：改名
        rs[2][i_op] = "删除"                                  # 第二行：删除
        buf = io.StringIO(newline="")
        csv.writer(buf).writerows(rs)
        filled = buf.getvalue()

        d2 = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                   "kind": "sheet_ops", "csv": filled})
        ck(d2["ok"], "生成预览成功", d2.get("msg"))
        ck(d2.get("renames") == 1 and d2.get("deletes") == 1,
           "★ 改名 1 条 + 删除 1 条，分开报数", (d2.get("renames"), d2.get("deletes")))
        ck(d2.get("skipped") == 2, "★ 没写的两行跳过（不是报错）", d2.get("skipped"))
        kinds = sorted(o["op"] for o in d2.get("ops") or [])
        ck(kinds == ["delete", "rename"], "★ 两种操作在同一份计划里", kinds)

        print("\n【3】执行：网盘上真的改了名、真的删了")
        r = api("/api/pan_apply", {"src": "remote", "ops": d2["ops"],
                                   "preview_id": d2["preview_id"],
                                   "total": d2["total"], "ondup": "skip"})
        ck(r["ok"], "执行成功", r.get("msg"))
        st = r.get("stat") or {}
        ck(st.get("rename") == 1, "改名 1 条", st.get("rename"))
        ck(st.get("delete") == 1, "★ 删除 1 条", st.get("delete"))
        left = sorted(f["path"] for f in pan.tree)
        ck(f"{SB}/书/三体.txt" in left, "★ 文件真的改名了", left)
        ck(f"{SB}/书/b.txt" not in left, "★ 标了删除的那个真没了", left)
        ck(f"{SB}/书/c.txt" in left and f"{SB}/书/d.pdf" in left,
           "★ 没写操作的那两行一个没动", left)

        print("\n【4】★ 危险写法一律拦在生成预览这一步")
        bad = [
            ("操作乱写", "操作,原路径,新文件名\n合并,/apps/x/书/c.txt,x.txt\n",
             "认不出"),
            ("改名没给新名", "操作,原路径,新文件名\n改名,/apps/x/书/c.txt,\n",
             "没填新文件名"),
            ("新名带斜杠", "操作,原路径,新文件名\n改名,/apps/x/书/c.txt,子/x.txt\n",
             "不能含斜杠"),
            ("路径出界", "操作,原路径,新文件名\n删除,/别的盘/书/c.txt,\n",
             "越界"),
        ]
        for name, text, want in bad:
            dd = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                       "kind": "sheet_ops", "csv": text})
            msg = " ".join(dd.get("errors") or []) + " " + str(dd.get("msg") or "")
            ck(want in msg, f"★ {name} → 拦下并说明了原因", msg[:90])
            ck((dd.get("total") or 0) == 0, f"★ {name} → 一条都没放行", dd.get("total"))

        print("\n【5】向后兼容：没有「操作」列的老表格照样整张按改名")
        p = webui.Handler._parse_sheet_csv(
            "原路径,新文件名\n/apps/x/书/a.txt,A2.txt\n/apps/x/书/b.txt,B2.txt\n", SB)
        ck([o["op"] for o in p["ops"]] == ["rename", "rename"],
           "★ 老两列表格 = 全部改名", [o["op"] for o in p["ops"]])
        ck(p["renames"] == 2 and p["deletes"] == 0, "统计也对", (p["renames"], p["deletes"]))

        print("\n【6】表格形态认得全")
        cases = [
            ("英文表头", "action,path,newname\nrename,/apps/x/a.txt,A2.txt\ndelete,/apps/x/b.txt,",
             1, 1),
            ("操作在最后一列", "原路径,新文件名,操作\n/apps/x/a.txt,A2.txt,改名\n/apps/x/b.txt,,删除",
             1, 1),
            ("删除专用两列", "操作,原路径\n删除,/apps/x/a.txt\n删除,/apps/x/b.txt", 0, 2),
            ("没有表头", "/apps/x/a.txt,A2.txt\n/apps/x/b.txt,B2.txt", 2, 0),
            ("GBK 乱码也没事（这里给 UTF-8）",
             "操作,原路径,新文件名\n改名,/apps/x/中文.txt,新中文.txt\n", 1, 0),
        ]
        for name, text, nren, ndel in cases:
            p = H._parse_sheet_csv(text, SB)
            ck(p["renames"] == nren and p["deletes"] == ndel and not p["errs"],
               f"★ {name}", (p["renames"], p["deletes"], p["errs"][:2]))

        print("\n【7】整张空表 / 全是留空的行 → 如实说没东西可做")
        dd = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                   "kind": "sheet_ops",
                                   "csv": "操作,原路径,新文件名\n,,,\n,,\n"})
        ck(dd["ok"] is False, "空表不给生成预览", dd.get("msg"))
        ck("没解析出" in (dd.get("msg") or ""), "★ 说清是没解析出操作，而不是表格坏了",
           dd.get("msg"))

        print("\n【8】预览复用：同表复用、执行过就不再复用")
        # 已经执行过的那份按设计不再被复用（网盘变了，拿它当当前状态是错的），
        # 所以换一张没跑过的表来验「同参数不重复解析」
        new_csv = ("操作,原路径,新文件名\n"
                   f"改名,{SB}/书/c.txt,C2.txt\n")
        dd = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                   "kind": "sheet_ops", "csv": new_csv})
        first_id = dd.get("preview_id")
        ck(dd["ok"] and dd.get("reused") is not True, "第一次解析出预览", dd.get("msg"))
        dd2 = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                    "kind": "sheet_ops", "csv": new_csv})
        ck(dd2.get("reused") is True, "★ 同一张表 → 直接复用（不重复解析）",
           (dd2.get("reused"), dd2.get("msg")))
        ck(dd2.get("preview_id") == first_id, "是同一份预览", None)
        # 执行掉它，再点一次应该是「重新解析」而不是复用
        r = api("/api/pan_apply", {"src": "remote", "ops": dd["ops"],
                                   "preview_id": first_id,
                                   "total": dd["total"], "ondup": "skip"})
        ck(r["ok"], "执行成功", r.get("msg"))
        dd3 = api("/api/pan_plan", {"path": SB, "src": "remote", "root": SB,
                                    "kind": "sheet_ops", "csv": new_csv})
        ck(dd3.get("reused") is not True,
           "★ 执行过之后同一张表不再复用（网盘已经变了）", dd3.get("reused"))

        webui.PROJ, webui.CONFIG, webui.PROGRESS = saved[0], saved[1], saved[2]
        webui.PIDFILE, webui.LOGFILE = saved[3], saved[4]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


try:
    main()
except Exception:
    import traceback
    traceback.print_exc()
    FAIL += 1

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
