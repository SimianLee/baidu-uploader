# -*- coding: utf-8 -*-
r"""界面冒烟：卡片 4 新增的「整理对象：百度网盘 / 本地文件夹」

真 Chrome 打开真服务，点的都是页面上那几个按钮——验的是**人能不能用**，
不是接口能不能调（接口那层在 _t_local_http 里已经验过了）：

  1. 默认网盘；切到本地后警告条露出来、目录框换成配置里的本地目录
  2. 点「浏览」列出来的是本机磁盘上真有的目录
  3. 生成预览 → 点执行 → 磁盘上的文件真的改了名
  4. 切回网盘：手里的本地预览作废（不然会把网盘路径当本地路径执行）
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
import local_fs as lf  # noqa: E402
import webui  # noqa: E402
from cdp_driver import UIBrowser  # noqa: E402

PORT = 8797
CDP_PORT = 9237
OK = FAIL = 0


def ck(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


tmp = Path(tempfile.mkdtemp(prefix="uilocal_"))
local = tmp / "书籍"
(local / "小说").mkdir(parents=True, exist_ok=True)
(local / "小说" / "a.txt").write_bytes(b"x" * 100)


class FakePan:
    """网盘侧的替身（本用例只拿它当「另一边」，不需要真树）"""

    sandbox = "/apps/root"

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        return [{"path": f"{self.sandbox}/小说", "name": "小说", "isdir": True,
                 "size": 0, "mtime": 1}]

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


PAN = FakePan()


def fake_pan(self, src="remote", root_=None):
    if str(src).lower() == "local":
        p = lf.LocalFiles(root_ or str(local))
        return p, p.sandbox
    return PAN, PAN.sandbox


webui.PROJ = tmp
webui.CONFIG = tmp / "config.json"
webui.CONFIG.write_text(json.dumps(
    {"app_key": "k", "secret_key": "s", "remote_dir": PAN.sandbox,
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

        print("【1】默认网盘：只有网盘那条说明，本地警告是藏着的")
        ck(js("$('srcRemote').classList.contains('on')") is True,
           "默认「百度网盘」高亮", None)
        ck(js("$('panLocalHint').classList.contains('hidden')") is True,
           "本地警告默认不显示", None)
        ck(js("$('panHint').classList.contains('hidden')") is False,
           "网盘那条说明在", None)

        print("\n【2】★ 切到「本地文件夹」：警告露出来、目录换成配置里的本地目录")
        js("setPanSrc('local')")
        ck(js("$('srcLocal').classList.contains('on')") is True,
           "「本地文件夹」高亮了", None)
        ck(js("$('panLocalHint').classList.contains('hidden')") is False,
           "★ 本地警告露出来了（删了不进回收站这件事必须写在前头）", None)
        ck("不可恢复" in js("$('panLocalHint').textContent"),
           "警告里写明不可恢复", None)
        ck(js("$('panHint').classList.contains('hidden')") is True,
           "网盘那条说明收起来了", None)
        ck(js("$('panPath').placeholder").startswith("D:"),
           "目录框的示例换成本地路径", js("$('panPath').placeholder"))
        ck(js("$('panPath').value").replace("\\", "/").lower()
           == str(local).replace("\\", "/").lower(),
           "★ 目录自动填成配置里的本地目录", js("$('panPath').value"))

        print("\n【3】★ 点「浏览」：列出来的是本机磁盘上真有的目录")
        js("panBrowse()")
        time.sleep(1.2)
        ck("小说" in js("$('panList').textContent"),
           "★ 子目录是从磁盘上扫出来的", js("$('panList').textContent")[:80])
        ck("列出 1 个子目录" in js("$('panMsg').textContent"),
           "提示写明列了多少", js("$('panMsg').textContent"))

        print("\n【4】★ 生成预览 → 执行：磁盘上的文件真的改了名")
        js("""$('rnMode').value='replace';panModeChange();
              $('rnFind').value='a';$('rnRepl').value='A';
              $('panRecursive').checked = true;""")   # 文件在子目录里，必须含子目录
        js("panPlan()")
        time.sleep(1.5)
        ck(js("$('panPreview').textContent").count("A.txt") >= 1,
           "★ 预览里出现了新名 A.txt", js("$('panPreview').textContent")[:120])
        ck(js("$('btnPanApply').disabled") is False, "「执行」按钮可点了", None)
        js("panApply()")
        time.sleep(2.0)
        names = os.listdir(local / "小说")
        ck("A.txt" in names and "a.txt" not in names,
           "★ 磁盘上真的改名了（NTFS 只认真实名字）", names)
        ck("改名" in js("$('panMsg').textContent"),
           "结果提示里有改名", js("$('panMsg').textContent")[:80])

        print("\n【4b】★ 浏览下钻不改整理范围；归档填到范围外会被当场说清")
        js(f"$('panPath').value = {json.dumps(str(local / '小说'))}; panBrowse();")
        time.sleep(1.2)
        ck("A.txt" in js("$('panList').textContent"),
           "下钻进了子目录", js("$('panList').textContent")[:60])
        ck(js("localRoot").replace("\\", "/").lower()
           == str(local).replace("\\", "/").lower(),
           "★ 整理范围仍是上一级（浏览下钻不把它收窄）", js("localRoot"))
        ck(js("$('panLocalScope').textContent").replace("\\", "/").lower()
           == str(local).replace("\\", "/").lower(),
           "★ 界面上把整理范围写出来给人看", js("$('panLocalScope').textContent"))
        js("panTab('organize'); $('ogMode').value='organize'; ogModeChange();")
        js(f"$('ogDest').value = {json.dumps(str(tmp / '范围外'))};")
        js("panPlan()")
        time.sleep(1.2)
        msg = js("$('panMsg').textContent")
        ck("整理范围之外" in msg, "★ 归档目录在范围外：当场说清，而不是执行时才报越界",
           msg)
        ck("操作目录" in msg, "★ 给出「把操作目录改成 X」这条明路", msg)
        js(f"$('ogDest').value = {json.dumps(str(local / '归档'))};")
        js("panPlan()")
        time.sleep(1.2)
        ck(js("panOps.length") == 1,
           "★ 归档目录挪回范围内就能正常生成", js("$('panMsg').textContent"))

        print("\n【4c】★ 手改操作目录：整理范围跟着换，生成与执行必须按同一个根")
        # 用户场景原样复刻：切到本地时目录默认是配置里那个（子目录），他手改成
        # 另一个目录后生成预览——生成和执行若各按各的根，整批会报「越界」
        up = tmp / "未整理"
        (up / "海子诗集").mkdir(parents=True, exist_ok=True)
        (up / "海子诗集" / "00.txt").write_bytes(b"x" * 60)
        js("panTab('organize'); $('ogMode').value='organize'; ogModeChange();")
        js(f"""$('panPath').value = {json.dumps(str(up))};
               $('ogDest').value = {json.dumps(str(up / '归档'))};
               $('panRecursive').checked = true;""")
        js("panPlan()")
        time.sleep(1.6)
        ck(js("localRoot").replace("\\", "/").lower()
           == str(up).replace("\\", "/").lower(),
           "★ 整理范围换到了手填的目录（不再是配置里那个默认值）", js("localRoot"))
        ck(js("$('panLocalScope').textContent").replace("\\", "/").lower()
           == str(up).replace("\\", "/").lower(),
           "★ 界面上的整理范围同步更新了", js("$('panLocalScope').textContent"))
        ck(js("panOps.length") == 1, "★ 归档计划生成成功（没被判成越界）",
           js("$('panMsg').textContent"))
        js("panApply()")
        time.sleep(2.2)
        ck((up / "归档").is_dir() and not (up / "海子诗集" / "00.txt").exists(),
           "★ 文件真的从磁盘上搬进了归档目录",
           [str(p.relative_to(up)) for p in up.rglob("*")][:6])
        ck("移动" in js("$('panMsg').textContent"),
           "结果提示里报了移动", js("$('panMsg').textContent")[:80])

        print("\n【5】★ 切回网盘：手里的本地预览必须作废")
        js("setPanSrc('remote')")
        ck(js("panOps.length") == 0,
           "★ 预览清单清空了（防止拿本地路径去网盘上执行）",
           js("panOps.length"))
        ck(js("$('panPreview').classList.contains('empty')") is True,
           "预览区回到空状态", None)
        ck(js("$('panPath').value") == PAN.sandbox,
           "目录填回网盘沙盒", js("$('panPath').value"))
        ck(js("$('panLocalHint').classList.contains('hidden')") is True,
           "本地警告又收起来了", None)

        print("\n【6】整页没有 JS 报错")
        errs = js("window.__e") or []
        ck(not errs, "★ 没有 JS 错误", errs[:3])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 46}\n通过 {OK} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
