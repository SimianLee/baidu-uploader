# -*- coding: utf-8 -*-
"""批量删除新增的「中文名」筛选：名字里没有汉字的可一键挑出来

网盘里反复上传攒下来的垃圾多半长成 `8f3c2a1b.pdf`、`2019_report.pdf` 这种
——一个汉字都没有。加这条筛选就是为了让这种能被整批挑走。反过来的风险也摆
在这儿：英文原版书名、纯数字号也「没有中文」，所以判定必须写明边界（汉字
才算，日文假名/韩文/全角字母都不算），脏参数一律按「不筛」处理。

离线部分验判定与筛选；HTTP 部分用真服务 + 假网盘验路由、标签、预览隔离，
以及真执行时确实把挑出来的那批删掉了。
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
import pan_tools as pt  # noqa: E402
import webui  # noqa: E402
import preview_store as ps  # noqa: E402

PORT = 8796
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


def F(name, size=10):
    return {"path": "/apps/root/" + name, "name": name, "size": size,
            "isdir": False}


# ---------------------------------------------------------------- 离线：判定
print("【1】has_cjk：汉字才算中文，其它一律不算")
for nm, want in [
        ("报告.pdf", True), ("我的书 (1).txt", True), ("簡體也能認.txt", True),
        ("《三体》.epub", True),          # 书名号是中文标点，汉字「三体」也在
        ("、【】.txt", True),              # 只有中文标点也算（没人会给英文文件起这种名）
        ("Steve Jobs.epub", False),
        ("8f3c2a1b21.pdf", False),       # 网盘最常见的哈希名：正是要清的那批
        ("2019_report.pdf", False),
        ("12345.txt", False),
        ("ＡＢＣ.txt", False),             # ★ 全角字母不是汉字
        ("ソウル.txt", False),             # 日文假名不是汉字
        ("한국어.txt", False),             # 韩文不是汉字
        ("Привет.pdf", False),            # 西里尔不是汉字
        ("", False)]:
    ck(pt.has_cjk(nm) is want, f"{nm or '(空名)'} ⇒ {'有' if want else '没有'}中文",
       pt.has_cjk(nm))

print("\n【2】cjk_mode：认不出来的值一律按 any（不筛）")
for raw, want in [("no", "no"), ("yes", "yes"), ("any", "any"), ("NO", "no"),
                  (" Yes ", "yes"), (None, "any"), ("", "any"), ("乱码", "any"),
                  ("nope", "any")]:
    ck(pt.cjk_mode({"cjk": raw}) == want, f"cjk={raw!r} ⇒ {want}",
       pt.cjk_mode({"cjk": raw}))
ck(pt.cjk_mode({}) == "any", "没这个键 ⇒ 不筛", pt.cjk_mode({}))

print("\n【3】filter_files：三种档位 + 和别的条件是「并且」")
FILES = [F("报告.pdf"), F("Steve Jobs.epub"), F("讲义(1).txt"), F("hash123.bin")]
names = lambda ops: sorted(o["name"] for o in ops)

ops = pt.build_delete_plan(FILES, {"cjk": "no"})
ck(names(ops) == ["Steve Jobs.epub", "hash123.bin"],
   "★ no：只留没有中文的", names(ops))
ops = pt.build_delete_plan(FILES, {"cjk": "yes"})
ck(names(ops) == ["报告.pdf", "讲义(1).txt"], "★ yes：只留有中文的", names(ops))
ops = pt.build_delete_plan(FILES, {"cjk": "any"})
ck(len(ops) == 4, "any：一个不筛", names(ops))
ops = pt.build_delete_plan(FILES, {"cjk": "脏值"})
ck(len(ops) == 4, "★ 脏参数按不筛，绝不拿它当「no」去删", names(ops))
ops = pt.build_delete_plan(FILES, {"cjk": "no", "exts": "bin"})
ck(names(ops) == ["hash123.bin"], "★ 跟后缀是并且关系", names(ops))
ops = pt.build_delete_plan(FILES, {"cjk": "no", "keywords": "Steve"})
ck(names(ops) == ["Steve Jobs.epub"], "★ 跟「文件名含」也是并且", names(ops))
ops = pt.build_delete_plan([], {"cjk": "no"})
ck(ops == [], "空列表不炸", ops)

print("\n【4】备份区里的东西照样排除")
BP = "/apps/root/_覆盖备份/20260101-101010"
ops = pt.build_delete_plan([F("hash123.bin"), {"path": BP + "/hash456.bin",
                                              "name": "hash456.bin",
                                              "size": 1}], {"cjk": "no"})
ck(len(ops) == 1 and ops[0]["name"] == "hash123.bin",
   "★ 备份区不参与筛选（不会把备份再删一遍）", [o["path"] for o in ops])


# ------------------------------------------------------------ HTTP：真服务
class FakePan:
    """网盘里混着中文名、英文书名、哈希名、以及一个目录（不该被挑中）"""
    sandbox = "/apps/root"

    def __init__(self):
        self.files = [
            (f"{self.sandbox}/报告.pdf", 100),
            (f"{self.sandbox}/讲义.txt", 100),
            (f"{self.sandbox}/Steve Jobs.epub", 100),
            (f"{self.sandbox}/2019_report.pdf", 100),
            (f"{self.sandbox}/8f3c2a1b21.bin", 100),
        ]
        self.dirs = [self.sandbox, f"{self.sandbox}/归档"]
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
        return len(ops), []

    def rename_batch(self, ops, ondup="skip", on_progress=None):
        self.calls.append(("rename", len(ops)))
        return len(ops), []

    def delete_batch(self, paths, on_progress=None):
        self.calls.append(("delete", len(paths)))
        self.deleted = list(paths)
        if on_progress:
            on_progress(len(paths), 0)
        return len(paths), []


PAN = FakePan()


def paths_of(d):
    return sorted(o["path"] for o in d.get("ops") or [])


def plan(filters, refresh=False):
    return api("/api/pan_plan", {"path": PAN.sandbox, "recursive": True,
                                 "kind": "delete", "filters": filters,
                                 "refresh": refresh})


def http_part():
    global PASS, FAIL
    tmp = Path(tempfile.mkdtemp(prefix="delcjk_"))
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
        webui.Handler._pan = lambda self, *a, **k: (PAN, PAN.sandbox)
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), webui.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            print("\n【5】cjk=no：挑出来的都是没汉字的")
            d = plan({"cjk": "no"})
            ck(d["ok"], "生成预览成功", d.get("msg"))
            got = paths_of(d)
            ck(got == [f"{PAN.sandbox}/2019_report.pdf",
                       f"{PAN.sandbox}/8f3c2a1b21.bin",
                       f"{PAN.sandbox}/Steve Jobs.epub"],
               "★ 三个没中文的进清单，两个中文的没进来", got)
            ck(d.get("total") == 3, "总数 3", d.get("total"))
            ck(d.get("cjk_hits") == 3, "★ 报出「没有中文的共 3 个」",
               d.get("cjk_hits"))
            ck(d.get("cjk") == "no", "档位回传给了前端", d.get("cjk"))
            lab = (ps.load_preview(tmp, d["preview_id"]) or {}).get("label") or ""
            ck("名字没有中文" in lab, "★ 预览标题写明按中文名筛的", lab)

            print("\n【6】cjk=yes：反过来只挑有汉字的")
            d2 = plan({"cjk": "yes"})
            got2 = paths_of(d2)
            ck(got2 == [f"{PAN.sandbox}/报告.pdf", f"{PAN.sandbox}/讲义.txt"],
               "★ 只有两个中文的", got2)
            ck(d2.get("cjk_hits") == 2, "报出「含中文的共 2 个」",
               d2.get("cjk_hits"))
            lab2 = (ps.load_preview(tmp, d2["preview_id"]) or {}).get("label") or ""
            ck("名字含中文" in lab2, "标题写明是含中文", lab2)

            print("\n【7】不填 / 脏值 ⇒ 一条不筛")
            d3 = plan({})
            ck(len(paths_of(d3)) == 5, "不填：5 个全在", paths_of(d3))
            d4 = plan({"cjk": "???"})
            ck(len(paths_of(d4)) == 5, "★ 脏值：5 个全在（不许当 no 去删）",
               paths_of(d4))
            ck(d4.get("cjk_hits") is None, "脏值不带中文统计", d4.get("cjk_hits"))

            print("\n【8】换档位必须重算，不许拿另一档的旧预览顶上")
            again = plan({"cjk": "no"})
            ck(again.get("reused") is True, "同档位第二次复用缓存",
               again.get("reused"))
            ck(again["preview_id"] != d2["preview_id"],
               "★ no 与 yes 是两份各自的预览",
               (again.get("preview_id"), d2.get("preview_id")))
            ck(paths_of(again) == got, "复用回来的清单仍是 no 那一批",
               paths_of(again))

            print("\n【9】真的执行掉：删的就是挑出来的那三个")
            PAN.calls.clear()
            PAN.deleted = []
            r = api("/api/pan_apply", {"ops": d["ops"], "ondup": "skip",
                                       "preview_id": d["preview_id"],
                                       "total": d["total"],
                                       "backup_delete": False})
            ck(r["ok"], "执行成功", r.get("msg"))
            st = r.get("stat") or {}
            ck(st.get("delete", 0) == 3, "★ 删掉 3 个", st.get("delete"))
            ck(sorted(PAN.deleted) == got, "★ 假网盘收到的正是那三个",
               PAN.deleted)
            ck(not any(k == "move" for k, _ in PAN.calls),
               "没勾备份 ⇒ 一次搬运都没有", PAN.calls)
        finally:
            srv.shutdown()
            for attr, val in zip(("PROJ", "CONFIG", "PROGRESS", "PIDFILE",
                                  "LOGFILE"), saved):
                setattr(webui, attr, val)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


http_part()
print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
