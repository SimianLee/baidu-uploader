# -*- coding: utf-8 -*-
r"""本次改动的界面冒烟：页签合并 + 合并重复的新下拉。

  1. 网盘整理只剩三个页签，「按表改名」不再是独立页签
  2. 改名模式切到「按表格改名」时：表格那一块显示、其它参数与「含子目录」隐藏
  3. 切回别的模式：那些参数又回来（不会永久消失）
  4. 整理方式切到「合并重复目录」时：「重复的文件怎么处理」下拉才出现
  5. 整页没有 JS 报错
"""
import json
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

PORT = 8791
CDP_PORT = 9231
OK = FAIL = 0


def ck(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


tmp = Path(tempfile.mkdtemp(prefix="uitab_"))
webui.PROJ = tmp
webui.CONFIG = tmp / "config.json"
webui.CONFIG.write_text(json.dumps({"remote_dir": "/apps/root"}), encoding="utf-8")
srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

try:
    with UIBrowser(f"http://127.0.0.1:{PORT}/", port=CDP_PORT) as b:
        def js(expr):
            return b.js(expr)

        time.sleep(2.0)
        # 先把窗口里的 JS 错误兜住
        js("window.__e=[];window.onerror=(m)=>{window.__e.push(String(m))}")

        print("【1】页签：只剩三个")
        tabs = js("[...document.querySelectorAll('.tabs .tab')].map(e=>e.textContent.trim())")
        ck("按表改名" not in tabs, "★ 不再有独立的「按表改名」页签", tabs)
        ck(tabs == ["批量改名", "整理归档", "批量删除"], "三个页签顺序正确", tabs)

        print("【2】改名模式下拉里有「按表格改名」")
        opts = js("[...$('rnMode').options].map(o=>o.textContent.trim())")
        ck(any("按表格改名" in o for o in opts), "★ 多了一个新的模式选项", opts)
        ck(len(opts) == 7, "六种模式 + 表格，共 7 个选项", len(opts))

        print("【3】★ 切到表格模式：只显示表格那一块")
        js("panModeChange()")                       # 先在默认模式下同步一次
        js("$('rnMode').value='csv';panModeChange()")
        st = js("""(() => {
          const hid = id => $(id).classList.contains('hidden');
          return {csv: hid('rnCsvBox'), find: hid('rnFindBox'), tpl: hid('rnTplBox'),
                  ext: hid('rnExtBox'), hint: hid('rnHint'), rec: hid('recBox')};
        })()""")
        ck(st["csv"] is False, "表格那一块显示出来了", st)
        ck(st["find"] and st["tpl"], "★ 其它模式的参数藏起来了", st)
        ck(st["ext"], "「只处理后缀」也藏起来了（表格模式下没意义）", st)
        ck(st["rec"], "★「含子目录」也藏起来了（表格模式不扫网盘）", st)

        print("【4】切回「查找替换」：那些参数都得回来")
        js("$('rnMode').value='replace';panModeChange()")
        st2 = js("""(() => {
          const hid = id => $(id).classList.contains('hidden');
          return {csv: hid('rnCsvBox'), find: hid('rnFindBox'), ext: hid('rnExtBox'),
                  rec: hid('recBox')};
        })()""")
        ck(st2["csv"] is True, "表格那一块收回去了", st2)
        ck(st2["find"] is False and st2["ext"] is False and st2["rec"] is False,
           "★ 参数和「含子目录」都恢复了", st2)

        print("【5】★ 整理方式切到「合并重复目录」才出现新下拉")
        js("panTab('organize')")
        js("$('ogMode').value='organize';ogModeChange()")
        ck(js("$('ogMergeHow').closest('.field').classList.contains('hidden')") is True,
           "归类移动时不显示", None)
        js("$('ogMode').value='merge';ogModeChange()")
        ck(js("$('ogMergeHow').closest('.field').classList.contains('hidden')") is False,
           "★ 合并重复时显示出来了", None)
        ck(js("$('ogMergeHow').value") == "dedupe",
           "默认就是「删除重复那份」", js("$('ogMergeHow').value"))
        opts2 = js("[...$('ogMergeHow').options].map(o=>o.textContent.trim())")
        ck(len(opts2) == 2, "两个选项", opts2)
        ck(js("$('recBox').classList.contains('locked')") is True,
           "递归仍然被锁（合并重复必须看整棵子树）", None)

        print("【5b】★ 合并重复：多了一层「处理哪一层」，文案跟着变")
        ck(js("$('ogMergeTarget').closest('.field').classList.contains('hidden')") is False,
           "★ 新下拉「处理哪一层重复」跟着合并重复一起露出来", None)
        ck(js("$('ogMergeTarget').value") == "both",
           "默认两层都处理", js("$('ogMergeTarget').value"))
        topts = js("[...$('ogMergeTarget').options].map(o=>o.value)")
        ck(topts == ["both", "dir", "file"], "三个选项齐全", topts)
        # 切到「只处理文件」：how 的第二个选项的措辞要换成文件层那句
        js("$('ogMergeTarget').value='file';ogMergeSync()")
        ck("改回主名" in js("$('ogMergeHow').options[1].textContent"),
           "★ 文件层的「不删」说成「改回主名」",
           js("$('ogMergeHow').options[1].textContent"))
        ck("大小" in js("$('ogHint').textContent"),
           "★ 说明里讲清了「大小必须一致」这条闸", None)
        ck("书(1).epub" in js("$('ogHint').textContent"),
           "说明举的是文件层的例子", js("$('ogHint').textContent")[:60])
        js("$('ogMergeTarget').value='dir';ogMergeSync()")
        ck("搬回主目录" in js("$('ogMergeHow').options[1].textContent"),
           "切回目录层 ⇒ 措辞回到「搬回主目录」",
           js("$('ogMergeHow').options[1].textContent"))
        ck("txt(1)" in js("$('ogHint').textContent"),
           "目录层的说明讲副本目录那一套", js("$('ogHint').textContent")[:60])
        js("$('ogMergeTarget').value='both';ogMergeSync()")
        ck("两层" in js("$('ogHint').textContent"),
           "两层都做时说明讲明「不会被处理两遍」", None)

        print("【5c】★ 只处理文件层：重名策略收起来（它在文件层不生效）")
        ck(js("$('dupBox').classList.contains('hidden')") is False,
           "两层都做时重名策略还在（目录层搬回撞名要用）", None)
        js("$('ogMergeTarget').value='file';ogMergeSync()")
        ck(js("$('dupBox').classList.contains('hidden')") is True,
           "★ 切到只处理文件 ⇒ 重名跳过/删除藏起来", None)
        ck("重名跳过 / 重名删除" in js("$('ogHint').textContent")
           or "重名" in js("$('ogHint').textContent"),
           "说明里写明了为什么用不上重名策略", None)
        js("$('ogMergeTarget').value='dir';ogMergeSync()")
        ck(js("$('dupBox').classList.contains('hidden')") is False,
           "切回目录层 ⇒ 重名策略回来", None)

        print("【6】切回改名页签：改名的显隐状态跟着恢复")
        js("panTab('rename')")
        ck(js("$('rnCsvBox').classList.contains('hidden')") is True,
           "★ 停在 replace 模式，表格块是藏着的（没被 organize 那边的显隐带乱）",
           None)
        ck(js("$('recBox').classList.contains('locked')") is False,
           "递归锁已解开", None)

        print("【7】整页无 JS 报错")
        errs = js("window.__e || []")
        ck(not errs, "没有 JS 异常", errs)
finally:
    srv.shutdown()

print(f"\n== 通过 {OK} / 失败 {FAIL} ==")
sys.exit(1 if FAIL else 0)
