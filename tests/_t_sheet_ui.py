# -*- coding: utf-8 -*-
r"""界面冒烟：卡片 4 新页签「导出 / 按表格操作」

真 Chrome 打开真服务，点的都是页面上那几个按钮。验的是**人能不能用**：

  1. 新页签能切过去，面板只显示它自己
  2. 点「导出文件清单」→ 浏览器真的落了一份 CSV（内容是盘上真有的文件）
  3. 把表格内容直接喂给前端（等于用户选文件）→ 生成预览 → 报出「改名 N / 删除 N」
  4. 点执行 → 磁盘上的文件真改了名、标了删除的真没了
  5. 整页没有 JS 报错
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r"C:\Users\freedo\.workbuddy\skills\cdp-ui-test\scripts")
import webui  # noqa: E402
from cdp_driver import UIBrowser  # noqa: E402

PORT = 8794
CDP_PORT = 9234
OK = FAIL = 0


def ck(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


tmp = Path(tempfile.mkdtemp(prefix="uisheet_"))
local = tmp / "书籍"
(local / "小说").mkdir(parents=True, exist_ok=True)
(local / "小说" / "a.txt").write_bytes(b"x" * 100)
(local / "小说" / "b.txt").write_bytes(b"x" * 100)
(local / "小说" / "c.pdf").write_bytes(b"x" * 100)

SANDBOX = "/apps/root"


class FakePan:
    """网盘侧的替身：本用例跑的是本地那条路，它只需要「存在且不出错」"""

    sandbox = SANDBOX

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        return []

    def list_files(self, path, recursive=True, on_progress=None,
                   should_stop=None, failures=None):
        return []

    def list_names(self, path):
        return set()

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


import local_fs as lf  # noqa: E402

PAN = FakePan()


def fake_pan(self, src="remote", root_=None):
    if str(src).lower() == "local":
        p = lf.LocalFiles(root_ or str(local))
        return p, p.sandbox
    return PAN, PAN.sandbox


webui.PROJ = tmp
webui.CONFIG = tmp / "config.json"
webui.CONFIG.write_text(json.dumps(
    {"app_key": "k", "secret_key": "s", "remote_dir": SANDBOX,
     "local_dir": str(local)}), encoding="utf-8")
webui.PROGRESS = tmp / "progress.json"
webui.PIDFILE = tmp / "upload.pid"
webui.LOGFILE = tmp / "upload.log"
webui.Handler._pan = fake_pan
srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

try:
    with UIBrowser(f"http://127.0.0.1:{PORT}/", port=CDP_PORT) as b:
        js = b.js
        time.sleep(2.2)
        js("window.__e=[];window.onerror=(m)=>{window.__e.push(String(m))}")

        print("【1】切到「导出 / 按表格操作」页签")
        js("setPanSrc('local')")
        js(f"$('panPath').value = {json.dumps(str(local))};")
        js("panTab('sheet')")
        ck(js("$('tabSheet').classList.contains('on')") is True, "页签高亮", None)
        ck(js("$('paneSheet').classList.contains('on')") is True, "面板显示出来", None)
        ck(js("$('paneRename').classList.contains('on')") is False,
           "改名面板收起来了", None)

        print("\n【2】★ 点「导出文件清单」：浏览器真的落了一份 CSV")
        js("""window.__got = null;
              const _dt = window.downloadText;
              window.downloadText = function(fn, txt){ window.__got = [fn, txt]; };""")
        js("panListExport()")
        time.sleep(1.6)
        got = js("window.__got")
        ck(bool(got), "导出按钮真的触发了下载", js("$('panMsg').textContent"))
        fn, text = (got or ["", ""])
        ck(str(fn).endswith(".csv") and str(fn).startswith("文件清单"),
           "★ 文件名是「文件清单_xxx.csv」", fn)
        rows = [r for r in (text or "").replace("\ufeff", "").splitlines() if r.strip()]
        ck(rows and rows[0].startswith("序号,操作,原路径,文件名,后缀"),
           "★ 表头就是约定的那 9 列", rows[0] if rows else "")
        joined = "\n".join(rows)
        ck("a.txt" in joined and "txt" in joined, "★ 文件名和后缀都写进去了", None)
        ck("c.pdf" in joined and "pdf" in joined, "★ 各种后缀分别认出来", None)
        ck(str(local).replace("\\", "/") in joined.replace("\\", "/"),
           "★ 路径是本机那个真目录", None)
        ck("已导出" in js("$('panMsg').textContent"), "提示报了导出条数",
           js("$('panMsg').textContent"))

        print("\n【3】★ 把这表格填两列再导回来：报出「改名 N / 删除 N」")
        # 模拟用户在 Excel 里给「操作」「新文件名」两列填了值（挑第 1、2 行）
        lines = rows[:]
        head = lines[0].split(",")
        i_op, i_new = head.index("操作"), head.index("新文件名")

        def setrow(i, op, new):
            cs = lines[i].split(",")
            cs[i_op], cs[i_new] = op, new
            lines[i] = ",".join(cs)
        setrow(1, "改名", "三体.txt")
        setrow(2, "删除", "")
        feed = "\r\n".join(lines) + "\r\n"
        js(f"sheetText = {json.dumps(feed)}")
        js("panPlan()")
        time.sleep(1.8)
        msg = js("$('panMsg').textContent")
        ck(js("panOps.length") == 2, "★ 预览 2 条（1 改名 + 1 删除）", msg)
        ck("改名 1" in msg and "删除 1" in msg,
           "★ 结果文案把改名/删除分开报（删的不可恢复，必须报清）", msg)
        ck(js("$('btnPanApply').disabled") is False, "「执行」可点", None)
        stat = js("$('shStat').textContent")
        ck("改名 1" in stat and "删除 1" in stat, "★ 页签里的状态行也写明两种操作", stat)

        print("\n【4】★ 点执行：磁盘上真改了名、标删除的真没了")
        before = sorted(p.name for p in (local / "小说").iterdir())
        js("panApply()")
        time.sleep(2.2)
        after = sorted(p.name for p in (local / "小说").iterdir())
        ck("三体.txt" in after, "★ 改名生效了", after)
        ck("b.txt" not in after, "★ 标了删除的那个真没了", (before, after))
        ck("c.pdf" in after, "★ 没写操作的那行一个没动", after)
        ck("改名" in js("$('panMsg').textContent")
           or "完成" in js("$('panMsg').textContent"),
           "结果提示正常", js("$('panMsg').textContent")[:80])

        print("\n【5】危险写法当场报出来，一条都不放行")
        bad = ("操作,原路径,新文件名\n"
               f"合并,{local}/小说/c.pdf,x.pdf\n")
        js(f"sheetText = {json.dumps(bad)}")
        js("panPlan()")
        time.sleep(1.4)
        msg = js("$('panMsg').textContent")
        ck("认不出" in msg, "★ 操作列乱写 → 当场说清认不出", msg)
        ck(js("panOps.length") == 0, "一条都没放行", None)

        print("\n【6】整页没有 JS 报错")
        errs = js("window.__e") or []
        ck(not errs, "★ 没有 JS 错误", errs[:3])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 46}\n通过 {OK} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
