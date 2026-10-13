# -*- coding: utf-8 -*-
"""面板「预览文件」功能的真浏览器（CDP）界面回归

服务端用内存假网盘（不碰真网盘、不用 Token、不写项目目录），浏览器是系统里真实的
Chrome——验的是「点了按钮之后界面到底对不对」：下拉里有没有东西、复用的提示有没有
出来、「重新生成」是不是真的绕过缓存、选了旧预览能不能执行、执行完有没有盖上
「已执行」。
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, r"C:\Users\freedo\.workbuddy\skills\cdp-ui-test\scripts")
import webui
from cdp_driver import UIBrowser

PORT = 8792
CDP_PORT = 9225
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = Path(__file__).resolve().parent / "shots"
OK = FAIL = 0


def ck(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class FakePan:
    sandbox = "/apps/root"

    def __init__(self):
        self.dirs = ["/apps/root", "/apps/root/novel", "/apps/root/other"]
        self.files = [
            ("/apps/root/novel/《青云志》作者：萧鼎.txt", 100),
            ("/apps/root/novel/青云志 - 萧鼎.txt", 150),
            ("/apps/root/novel/剑来》by：烽火戏诸侯.txt", 200),
            ("/apps/root/novel/[玄幻]诛仙（全本）.txt", 300),
            ("/apps/root/novel/雪中悍刀行 - 烽火戏诸侯.txt", 400),
            ("/apps/root/other/a.tmp", 10),
        ]
        self.inject_failures = 0     # >0 时模拟「有 N 个目录列不开」，见 list_dir

    @staticmethod
    def _rec(path, isdir, size=0):
        return {"path": path, "name": path.rsplit("/", 1)[-1], "size": size,
                "isdir": isdir, "mtime": 1758600000}

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        # failures 是真实现里收集「列不开的子目录」用的。这个假网盘一次性返回整棵树，
        # 没有逐个目录列的过程，没法真模拟；inject_failures>0 时往 failures 里塞一条，
        # 专门用来在真浏览器里验证前端的「⚠ N 个目录打不开」提示确实渲染出来了
        p = path.rstrip("/")
        out = []
        for e in ([self._rec(d, True) for d in self.dirs]
                  + [self._rec(a, False, s) for a, s in self.files]):
            if e["path"] == p or not e["path"].startswith(p + "/"):
                continue
            if recursive or "/" not in e["path"][len(p) + 1:]:
                out.append(e)
        if failures is not None and self.inject_failures:
            for i in range(self.inject_failures):
                failures.append({"path": f"/apps/root/novel/打不开的目录{i+1}",
                                 "errno": -9, "msg": "文件不存在"})
        if on_progress:
            on_progress(1, sum(1 for x in out if not x["isdir"]), p)
        return out

    def list_files(self, path, recursive=True, on_progress=None, should_stop=None,
                   failures=None):
        return [e for e in self.list_dir(path, recursive=recursive,
                                         on_progress=on_progress, failures=failures)
                if not e["isdir"]]

    def mkdir(self, path):
        return True

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        if on_progress:
            on_progress(len(ops), 0)
        return len(ops), []

    def move_batch(self, ops, ondup="skip", on_progress=None):
        if on_progress:
            on_progress(len(ops), 0)
        return len(ops), []

    def delete_batch(self, paths, on_progress=None):
        if on_progress:
            on_progress(len(paths), 0)
        return len(paths), []


PAN = FakePan()


def main():
    tmp = Path(tempfile.mkdtemp(prefix="pvui_"))
    # 这几个常量在 import 时就定死了，只换 PROJ 并不够——PROGRESS / PIDFILE / LOGFILE
    # 仍指着**真实项目目录**，后果很实际：
    #   ① 页面自己每 2.5s 轮询 /api/progress，会去读真实的上传进度文件；老李真在跑上传时
    #      这一读正好能撞出 WinError 5，把人家几千个文件的上传打崩（这 bug 就是这么来的）；
    #   ② PIDFILE 读到真实 PID → is_running() 变 True → 按钮状态被外部环境影响，断言全乱。
    # 所以一律挪到临时区，测完还原。
    real = (webui.PROJ, webui.CONFIG, webui.HTMLFILE,
            webui.PROGRESS, webui.PIDFILE, webui.LOGFILE)
    webui.PROJ = tmp
    webui.CONFIG = tmp / "config.json"
    webui.HTMLFILE = real[2]
    webui.PROGRESS = tmp / "progress.json"
    webui.PIDFILE = tmp / "upload.pid"
    webui.LOGFILE = tmp / "upload.log"
    webui.CONFIG.write_text(json.dumps(
        {"app_key": "k", "secret_key": "s", "local_dir": str(tmp),
         "remote_dir": PAN.sandbox, "pre_rename": "none"}), encoding="utf-8")
    webui.Handler._pan = lambda self: (PAN, PAN.sandbox)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for _ in range(30):                     # 等端口起来
        try:
            urllib.request.urlopen(BASE + "/api/preview_list", timeout=2).read()
            break
        except Exception:
            time.sleep(0.2)

    try:
        with UIBrowser(BASE + "/", port=CDP_PORT) as b:

            def js(expr):
                return b.js(expr)

            def wait(expr, label, timeout=20):
                return b.wait_for(expr, timeout=timeout, label=label)

            def stable_js(expr, rounds=4, gap=0.3):
                """对一个异步更新的值反复取值，直到连续两次相同才返回

                pvRefresh() 是异步 fetch，DOM 更新滞后于「plan 已生成」这个信号。
                直接读会拿到旧值 —— 断言「复用不新增预览文件」就是这样偶发红的：
                n_before 读到旧的 5，列表异步刷成 6，6 != 5 就判失败。
                """
                import time as _t
                prev = None
                for _ in range(rounds):
                    cur = js(expr)
                    if prev is not None and cur == prev:
                        return cur
                    prev = cur
                    _t.sleep(gap)
                return prev

            def rect(sel):
                return js(f"""(() => {{
                    const e = document.querySelector({sel!r});
                    if (!e) return null;
                    e.scrollIntoView({{block:'center'}});
                    const r = e.getBoundingClientRect();
                    return {{x:r.x+r.width/2, y:r.y+r.height/2}};
                }})()""")

            def click(sel):
                r = rect(sel)
                if not r:
                    raise RuntimeError(f"找不到元素 {sel}")
                b.call("Input.dispatchMouseEvent", type="mouseMoved", x=r["x"], y=r["y"])
                b.call("Input.dispatchMouseEvent", type="mousePressed", x=r["x"],
                       y=r["y"], button="left", clickCount=1)
                b.call("Input.dispatchMouseEvent", type="mouseReleased", x=r["x"],
                       y=r["y"], button="left", clickCount=1)
                # 把指针挪开：:hover 会污染「点击之后」的样式断言
                b.call("Input.dispatchMouseEvent", type="mouseMoved", x=3, y=3)

            def set_select(sel, value):
                """设下拉值并派发 change（用 .value= 是不会触发 onchange 的）"""
                js(f"""(() => {{ const s = document.querySelector({sel!r});
                        s.value = {value!r};
                        s.dispatchEvent(new Event('change')); }})()""")

            def clip_shot(js_rect, path):
                r = js(js_rect)
                ox = js("window.scrollX") or 0
                oy = js("window.scrollY") or 0
                clip = {"x": r["x"] + ox - 8, "y": r["y"] + oy - 8,
                        "width": r["w"] + 16, "height": r["h"] + 16, "scale": 2}
                data = b.call("Page.captureScreenshot", format="png",
                              clip=clip)["data"]
                os.makedirs(str(SHOTS), exist_ok=True)
                with open(str(SHOTS / path), "wb") as f:
                    f.write(base64.b64decode(data))

            print("【1】控件都在")
            ck(wait("!!document.getElementById('pvList')", "pvList"), "预览文件下拉框存在")
            for i, name in [("btnPanPlan", "生成预览"), ("btnPanFresh", "重新生成"),
                            ("btnPvDel", "删除这份"), ("btnPanApply", "执行")]:
                ck(js(f"!!document.getElementById('{i}')"), f"{name} 按钮存在（#{i}）")
            ck(js("document.getElementById('btnPanFresh').textContent.trim()") == "重新生成",
               "「重新生成」文案正确")
            ck("还没有存下来的预览" in js("document.getElementById('pvHint').textContent"),
               "空列表时的提示说明白了去哪找预览")
            ck(js("document.getElementById('pvList').options.length") == 1
               and js("document.getElementById('pvList').value") == "",
               "空列表时下拉里只有一条占位")
            ck(js("document.getElementById('btnPvDel').disabled") is True,
               "没选中东西时「删除这份」是灰的")

            print("【2】点「生成预览」→ 落盘 + 下拉出现一份")
            # 改名模式默认是「查找替换」；要测只保留书名得先切过去（切完会重设输入框）
            set_select("#rnMode", "title")
            ck(js("document.getElementById('rnHint').textContent").find("书名号") >= 0,
               "切到「只保留书名」后提示跟着变")
            # 「含子目录」默认是关的，而测试数据都放在 /apps/root/novel 下，
            # 不勾上就会扫出 0 条（这本身也是真实使用里最容易踩的坑）
            js("""(() => { const c = document.getElementById('panRecursive');
                    c.checked = true; c.dispatchEvent(new Event('change')); })()""")
            ck(js("document.getElementById('recChip').classList.contains('on')"),
               "勾上「含子目录」后 chip 点亮")
            click("#btnPanPlan")
            ck(wait("panOps.length > 0 && !document.getElementById('btnPanPlan').disabled",
                    "预览生成完毕"), "预览生成完成")
            opts = js("""[...document.getElementById('pvList').options]
                           .map(o => ({v:o.value, t:o.text}))""")
            ck(len(opts) == 1 and opts[0]["v"] != "",
               "下拉里多出 1 份真预览（不是占位条）", opts)
            ck(js("document.getElementById('btnPvDel').disabled") is False, "「删除这份」点亮了")
            ck(js("document.getElementById('btnPanApply').disabled") is False, "「执行」点亮了")
            opt = opts[0]["t"]
            ck("批量改名" in opt and "只保留书名" in opt and "条" in opt and "含子目录" in opt,
               "下拉文案是人话（模式 + 条数 + 扫描范围）", opt)
            hint = js("document.getElementById('pvHint').textContent")
            ck("存了" in hint and "previews/" in hint, "提示里说明存到哪、存了几份", hint)
            msg = js("document.getElementById('panMsg').textContent")
            ck("已存为预览文件" in msg, "结果提示写明「已存为预览文件」", msg)
            prev = js("document.getElementById('panPreview').textContent")
            ck("青云志" in prev and "(2)" in prev, "预览区列出撞名编号结果")
            id1 = js("document.getElementById('pvList').value")
            clip_shot("""(() => {
                const a = document.getElementById('btnPanPlan').closest('.btnbar')
                          .getBoundingClientRect();
                const c = document.getElementById('pvList').closest('.row')
                          .getBoundingClientRect();
                return {x:Math.min(a.x,c.x), y:Math.min(a.y,c.y),
                        w:Math.max(a.width,c.width),
                        h:Math.max(a.bottom,c.bottom)-Math.min(a.y,c.y)};
            })()""", "preview_row.png")

            print("【3】再点一次 → 复用，不重扫（下拉不增加）")
            click("#btnPanPlan")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('复用') >= 0",
                    "复用提示"), "出现「复用」提示")
            ck(js("document.getElementById('pvList').options.length") == 1
               and js("document.getElementById('pvList').value") == id1,
               "没有多出新预览文件，选中的还是同一份（说明真的没重扫）")
            ck("♻" in js("document.getElementById('panPreview').textContent"),
               "预览区标出这份是复用来的")
            m = js("document.getElementById('panMsg').textContent")
            ck("没有重新扫描网盘" in m, "写明「没有重新扫描网盘」", m)

            print("【4】点「重新生成」→ 绕过缓存，多一份")
            click("#btnPanFresh")
            ck(wait("document.getElementById('pvList').options.length === 2",
                    "新的一份"), "「重新生成」后下拉里变成 2 份")
            ck(js("document.getElementById('pvList').value") != id1,
               "选中的换成了新存的那一份")
            ck("复用" not in js("document.getElementById('panMsg').textContent"),
               "这一份是重扫的，提示里没有「复用」")
            ck("已存为预览文件" in js("document.getElementById('panMsg').textContent"),
               "重扫的结果同样存了一份")

            print("【5】切页签 → 清空当前载入的计划")
            click("#tabDelete")
            ck(wait("!document.getElementById('paneDelete').classList.contains('hidden')",
                    "删除面板"), "切到「批量删除」页签")
            ck(js("document.getElementById('btnPanApply').disabled") is True,
               "换页签后「执行」回到禁用（不会拿着改名计划去执行删除）")
            ck(js("document.getElementById('pvList').options.length") >= 2,
               "预览文件列表跨页签保留")
            click("#tabRename")
            ck(wait("!document.getElementById('paneRename').classList.contains('hidden')",
                    "改名面板"), "切回「批量改名」")

            print("【6】从下拉里挑一份旧的 → 载入 → 执行")
            js("""(() => { const s = document.getElementById('pvList');
                   s.value = s.options[1].value;              // 挑「仅本级/旧」那一份
                   s.dispatchEvent(new Event('change')); })()""")
            ck(wait("panOps.length > 0", "载入旧预览"), "选中即载入，panOps 有内容")
            ck("来自预览文件" in js("document.getElementById('panPreview').textContent"),
               "预览区标明这是从预览文件载入的")
            ck("已载入" in js("document.getElementById('panMsg').textContent"),
               "提示写明「已载入」", js("document.getElementById('panMsg').textContent"))
            ck(js("document.getElementById('btnPanApply').disabled") is False,
               "载入后「执行」可点")
            js("window.__confirms.length = 0")
            click("#btnPanApply")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('完成') >= 0",
                    "执行完成", timeout=25), "执行完成")
            ck(len(js("window.__confirms")) == 1 and "即将" in js("window.__confirms")[0],
               "执行前有二次确认（文案含「即将」）", js("window.__confirms"))
            # 下拉里那一份应该被盖上「已执行」
            ck(wait("document.getElementById('pvList').textContent.indexOf('已执行') >= 0",
                    "已执行戳"), "下拉里显示「已执行」")
            ck(js("document.getElementById('pvList').selectedOptions[0].text")
               .find("已执行") >= 0, "执行完选中的正是刚执行的那份（直接看得见结果）")
            hint = js("document.getElementById('pvHint').textContent")
            ck("已执行过" in hint, "提示里说明这些不会再被复用", hint)
            idExec = js("document.getElementById('pvList').value")

            print("【7】再执行一份「已经执行过」的预览 → 必须额外警告")
            set_select("#pvList", idExec)
            ck(wait("panOps.length > 0", "再载入"), "载入那份已执行过的预览")
            m = js("document.getElementById('panMsg').textContent")
            ck("已经执行过一次" in m, "提示里点名「已经执行过一次」", m)
            ck("⚠ 这份已经执行过" in js("document.getElementById('panPreview').textContent"),
               "预览区也标了出来")
            js("window.__confirms.length = 0")
            click("#btnPanApply")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('完成') >= 0",
                    "二次执行完成", timeout=25), "二次执行完成")
            c = js("window.__confirms")[0]
            ck("已经执行过一次" in c and "确定还要继续吗" in c,
               "确认框里明确警告了重复执行", c)

            print("【8】清理已执行 → 只剩没执行过的")
            click("#btnPanPlan")            # 顺手确认这一路走来面板还能正常用
            ck(wait("panOps.length > 0", "再次生成"), "重新生成仍然正常")
            # 列表是异步刷新的，必须等它稳定后再取基准值，否则会撞上未刷新的旧值
            n_before = stable_js("document.getElementById('pvList').options.length")
            js("window.__confirms.length = 0")
            click("#btnPanPlan")            # 与上面同参数 → 走复用
            ck(wait("document.getElementById('panMsg').textContent.indexOf('复用') >= 0",
                    "复用"), "同参数复用正常")
            ck(stable_js("document.getElementById('pvList').options.length") == n_before,
               "复用不新增预览文件")
            js("""[...document.querySelectorAll('.btnbar button')]
                   .find(b => b.textContent.trim() === '清理已执行').click()""")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('清理') >= 0",
                    "清理提示"), "点「清理已执行」有反馈")
            ck("已执行" not in js("document.getElementById('pvList').textContent"),
               "列表里再没有「已执行」的条目")

            print("【9】删光 → 回到占位状态")
            for _ in range(12):
                if not js("document.getElementById('pvList').value"):
                    break
                n = js("document.getElementById('pvList').options.length")
                click("#btnPvDel")
                # 删掉最后一份时列表会退回「占位那一条」——条数从 1 变成 1（因为占位条
                # 本身也占一条），所以判据要把「已回到占位」也算成功，否则白等一次超时
                wait("document.getElementById('pvList').value === '' || "
                     f"document.getElementById('pvList').options.length < {n}",
                     "删掉一份", timeout=8)
            ck(js("document.getElementById('pvList').value") == "", "删光后回到占位条")
            ck(js("document.getElementById('pvList').options.length") == 1, "只剩占位那一条")
            ck(js("document.getElementById('btnPvDel').disabled") is True,
               "没东西可删时按钮回灰")
            ck("还没有存下来的预览" in js("document.getElementById('pvHint').textContent"),
               "提示也回到「还没有」那一版")
            ck(js("document.getElementById('btnPanApply').disabled") is True, "「执行」禁用")

            print("【10】删光之后还能正常再来一轮")
            click("#btnPanPlan")
            ck(wait("panOps.length > 0 && document.getElementById('pvList').value !== ''",
                    "再来一轮"), "删光后重新生成一切正常")
            ck(js("document.getElementById('btnPanFresh').disabled") is False,
               "「重新生成」按钮也恢复可点")

            print("【11】有目录打不开时，界面要如实说出来（不许假装扫全了）")
            PAN.inject_failures = 1
            click("#btnPanFresh")            # 重新生成，绕过旧快照
            ck(wait("document.getElementById('panMsg').textContent.indexOf('打不开') >= 0",
                    "出现提示", timeout=20), "结果提示里出现「目录打不开」")
            msg = js("document.getElementById('panMsg').textContent")
            ck("1 个目录打不开" in msg, "数量说对了", msg)
            ck("结果不完整" in msg, "明说「结果不完整」", msg)
            ck("打不开的目录1" in msg, "点名了是哪个目录", msg)
            ck("打不开" in js("document.getElementById('panPreview').textContent"),
               "预览区的汇总行也带上这条", js("document.getElementById('panPreview').textContent")[:160])
            # 把这条提示拍下来存档：这是这轮改动最直观的成果
            clip_shot("""(() => {
                const r = document.getElementById('panMsg').getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height};
            })()""", "skip_warn.png")

            clip_shot("""(() => {
                const rows = document.getElementById('btnPanPlan')
                    .closest('.card').querySelectorAll('.row');
                const r = rows[rows.length - 1].getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height};
            })()""", "preview_card.png")
            b.shot(str(SHOTS / "preview_page.png"))

            print("【11b】清理空文件夹（已挪到批量删除页签）：说清「无法确认 → 已排除、不会删」")
            click("#tabDelete")
            ck(wait("document.getElementById('dlTarget') !== null", "删什么下拉"),
               "切到「批量删除」页签")
            js("document.getElementById('dlTarget').value='empty'; dlModeChange();")
            ck(wait("document.getElementById('dlExts').closest('.field').classList.contains('hidden')"
                    " && !document.getElementById('edSkip').closest('.field').classList.contains('hidden')",
                    "清理空文件夹的输入框"), "切方式后只留下「跳过名字含」")
            ck(js("document.getElementById('panRecursive').disabled") is True,
               "递归开关被锁死（这个方式必须递归）")
            ck(js("document.getElementById('dupBox').classList.contains('hidden')") is True,
               "删除类方式不显示「重名跳过/覆盖」（跟它没关系）")
            click("#btnPanPlan")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('打不开') >= 0",
                    "空目录提示", timeout=20), "空文件夹页签也报了「目录打不开」")
            m2 = js("document.getElementById('panMsg').textContent")
            ck("已排除" in m2 and "不会删" in m2,
               "措辞说清它不会被当成空目录删掉（这是安全红线）", m2)

            print("【11c】整棵树 0 文件 → 提醒这是「纯目录骨架」，别顺手删了分类结构")
            saved_files = list(PAN.files)
            PAN.files = []                  # 造一个「只有目录、没有文件」的树
            click("#btnPanFresh")           # 必须重扫：指纹不含数据状态，不然会复用旧结果
            ck(wait("document.getElementById('panMsg').textContent.indexOf('纯目录骨架') >= 0",
                    "骨架提醒", timeout=20), "提示里出现「纯目录骨架」")
            m3 = js("document.getElementById('panMsg').textContent")
            ck("分类结构会一起消失" in m3, "说清了删掉的后果", m3)
            clip_shot("""(() => {
                const r = document.getElementById('panMsg').getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height};
            })()""", "skeleton_warn.png")
            PAN.files = saved_files         # 还原假网盘

            print("【12a】合并重复 · 默认「删除重复那份」：独有的搬回，重复的删掉")
            saved_dirs, saved_files = list(PAN.dirs), list(PAN.files)
            PAN.dirs = ["/apps/root", "/apps/root/txt", "/apps/root/txt(1)"]
            PAN.files = [
                ("/apps/root/txt/斗破苍穹.txt", 100),
                ("/apps/root/txt(1)/斗破苍穹.txt", 110),      # 重复 → 删掉
                ("/apps/root/txt(1)/武动乾坤.txt", 120),      # 独有 → 搬回
            ]
            click("#tabOrganize")     # 上一段停在「批量删除」页签，先回到整理页签
            ck(wait("document.getElementById('ogMode') !== null", "整理方式下拉"),
               "回到「整理归档」页签")
            js("document.getElementById('ogMode').value='merge'; ogModeChange();")
            ck(js("document.getElementById('panRecursive').disabled") is True,
               "递归开关被锁死（合并必须递归）")
            ck(js("document.getElementById('dupBox').classList.contains('hidden')") is False,
               "合并会往主目录里写名字，所以「重名跳过/删除」露出来了")
            ck(js("document.getElementById('ogMergeHow').value") == "dedupe",
               "★ 默认就是「删除重复那份」",
               js("document.getElementById('ogMergeHow').value"))
            click("#btnPanPlan")
            ck(wait("document.getElementById('panPreview').textContent.indexOf('移动') >= 0",
                    "合并预览", timeout=20), "预览里出现了移动操作")
            stats = js("document.getElementById('pvStats').textContent")
            ck("重复目录对" in stats, "统计卡里有「重复目录对」", stats)
            ck("搬回主目录" in stats and "删重复文件" in stats,
               "★ 搬回的和删掉的分开列（不再只有一个总数）", stats)
            pv = js("document.getElementById('panPreview').textContent")
            ck("斗破苍穹" in pv and "武动乾坤" in pv, "两个文件都在预览里", pv[:120])
            ck(pv.count("删") >= 1, "重复的那份画成了删除", pv[:160])
            msg12 = js("document.getElementById('panMsg').textContent")
            ck("待删除" in msg12, "提示里说清了会删掉几个", msg12[:160])

            print("【12b】切成「全部搬回主目录」：撞名的编号，一条都不删")
            js("document.getElementById('ogMergeHow').value='move'")
            click("#btnPanPlan")
            ck(wait("document.getElementById('panPreview').textContent"
                    ".indexOf('斗破苍穹 (2)') >= 0", "编号出现", timeout=20),
               "★ 切回 move 后撞名的编成 斗破苍穹 (2).txt",
               js("document.getElementById('panPreview').textContent")[:200])
            stats = js("document.getElementById('pvStats').textContent")
            ck("待搬文件" in stats, "统计卡回到「待搬文件」口径", stats)
            # 测完拨回默认再做执行那一段
            js("document.getElementById('ogMergeHow').value='dedupe'")
            click("#btnPanPlan")
            ck(wait("document.getElementById('panPreview').textContent"
                    ".indexOf('移动') >= 0", "默认档预览", timeout=20), "回到默认档")
            # 执行 → 战果统计卡 + 预览区清空（无失败）
            click("#btnPanApply")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('完成') >= 0",
                    "执行完成", timeout=20), "合并执行完成")
            res = js("document.getElementById('pvStats').textContent")
            ck("移动" in res, "战果统计卡显示移动数", res)
            b.shot(str(SHOTS / "merge_tab.png"))
            PAN.dirs, PAN.files = saved_dirs, saved_files

            click("#tabRename")
            ck(wait("!document.getElementById('paneRename').classList.contains('hidden')",
                    "切回"), "切回「批量改名」")
            PAN.inject_failures = 0

            print("【13】改名「只处理后缀」输入框")
            ck(js("!!document.getElementById('rnExts')"), "改名页签里有「只处理后缀」输入框")
            # 填 txt → 生成 → 结果里只应有 txt（假网盘里 other/a.tmp 必须被排除）
            js("""(() => { const e = document.getElementById('rnExts');
                    e.value = 'txt'; e.dispatchEvent(new Event('input')); })()""")
            click("#btnPanFresh")           # 强制重扫，免得复用上一份同参数的
            ck(wait("document.getElementById('btnPanPlan').disabled === false "
                    "&& document.getElementById('panMsg').textContent.length > 0",
                    "带后缀筛选的预览", timeout=20), "生成完成")
            pv = js("document.getElementById('panPreview').textContent")
            ck("a.tmp" not in pv, "★ 预览里没有 tmp（被后缀筛掉了）", pv[:160])
            ck("只处理" in js("document.getElementById('panMsg').textContent")
               or "只处理" in pv, "提示里写明「只处理 txt 后缀」",
               js("document.getElementById('panMsg').textContent")[:160])
            ck("（txt）" in js("document.getElementById('pvList').selectedOptions[0].text"),
               "下拉标题标出后缀", js("document.getElementById('pvList').selectedOptions[0].text"))
            js("""(() => { const e = document.getElementById('rnExts');
                    e.value = ''; e.dispatchEvent(new Event('input')); })()""")

            print("【14】预览明细：表格查看 + 导出 CSV")
            for i, name in [("btnPvTable", "查看全部"), ("btnPvCsv", "导出表格")]:
                ck(js(f"!!document.getElementById('{i}')"), f"{name} 按钮存在（#{i}）")
            # 回归守卫：遮罩是隐藏的，绝不能盖在页面上挡住点击（.mask 自带 display，
            # 曾经把 .hidden 的 display:none 盖掉，害得所有按钮都点不动）
            ck(js("getComputedStyle(document.getElementById('pvMask')).display") == "none",
               "★ 明细弹层平时是隐藏的（不会挡住页面）",
               js("getComputedStyle(document.getElementById('pvMask')).display"))
            click("#btnPvTable")
            ck(wait("!document.getElementById('pvMask').classList.contains('hidden')",
                    "弹层打开"), "点「查看全部」打开表格")
            ck(wait("document.querySelectorAll('#pvTblBody tr').length > 0", "表格有行"),
               "表格里列出了操作")
            n0 = js("document.querySelectorAll('#pvTblBody tr').length")
            ck("共" in js("document.getElementById('pvTblPage').textContent"),
               "页脚写明共几条、第几页", js("document.getElementById('pvTblPage').textContent"))
            ck("完整计划" in js("document.getElementById('pvTblSrc').textContent"),
               "写明数据来自完整计划（能看到全部）",
               js("document.getElementById('pvTblSrc').textContent"))
            # 搜一个肯定搜不到的词 → 行数归零（说明搜索真的走了后端）
            js("document.getElementById('pvTblQ').value = '绝不可能出现的名字zzz'")
            click("#pvTblQ")            # 聚焦，方便按回车
            js("pvTablePage(0)")
            ck(wait("document.getElementById('pvTblEmpty').textContent.length > 0",
                    "搜索无结果", timeout=10), "搜不到时给出「没有匹配的操作」")
            js("document.getElementById('pvTblQ').value = ''"); js("pvTablePage(0)")
            ck(wait(f"document.querySelectorAll('#pvTblBody tr').length === {n0}",
                    "清空搜索后恢复"), "清空搜索后行数恢复")
            clip_shot("""(() => {
                const r = document.querySelector('#pvMask .maskbox').getBoundingClientRect();
                return {x:r.x, y:r.y, w:r.width, h:r.height};
            })()""", "preview_table.png")
            # Esc 关闭
            b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape",
                   code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
            b.call("Input.dispatchKeyEvent", type="keyUp", key="Escape",
                   code="Escape", windowsVirtualKeyCode=27, nativeVirtualKeyCode=27)
            ck(wait("document.getElementById('pvMask').classList.contains('hidden')",
                    "Esc 关闭"), "Esc 能关掉弹层")
            # 导出 CSV：浏览器下载不好断言，看提示里有没有如实报出条数
            js("pvExportCsv()")
            ck(wait("document.getElementById('panMsg').textContent.indexOf('已导出') >= 0",
                    "导出完成", timeout=15), "点「导出表格」后报告导出条数")
            ck(".csv" in js("document.getElementById('panMsg').textContent"),
               "提示里带出 .csv 文件名", js("document.getElementById('panMsg').textContent"))

            b.drain(1.2)
            real_errors = [e for e in b.errors
                           # 浏览器自动请求 favicon.ico，页面没放图标就是一条 404，
                           # 与被测脚本无关，不滤掉会让「零报错」恒为假
                           if "favicon.ico" not in e.lower()]
            ck(not real_errors, "全程没有 JS 报错", real_errors)

        print(f"\n== 通过 {OK} / 失败 {FAIL} ==")
        return 1 if FAIL else 0
    finally:
        srv.shutdown()
        (webui.PROJ, webui.CONFIG, webui.HTMLFILE,
         webui.PROGRESS, webui.PIDFILE, webui.LOGFILE) = real
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
