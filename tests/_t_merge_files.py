# -*- coding: utf-8 -*-
"""重复**文件**层的合并：A(1).txt / A(2).txt 合回 A.txt

目录那一层（_t_merge_dedupe.py）只管 txt(1)→txt 这种整个目录的副本；日常更常见
的是夹在同一层里的 `书(1).epub` `书(2).epub`，以前那条路一条都不认识——
这就是「合并重复功能看着有、其实没生效」的那半边。

【1】主名已在 ⇒ 多余副本按下拉处置（dedupe 删掉 / move 原样留着）
【2】★ 主名缺席但有两兄弟 ⇒ 序号最小的上位（改回主名）
【3】★ 大小不一致 ⇒ 当成成套资料放行，一个不动（删错比多留严重）
【4】孤零零一个 X(1) ⇒ 没有旁证，不许动它
【5】target 路由：file / dir / both，认不出来的按 both（绝不跳过某一层）
【6】两层一起做时，目录层碰过的子树文件层绕开（同一批文件不许被安排两次）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pan_tools as pt  # noqa: E402

PASS = FAIL = 0
SB = "/apps/baidu_uploader"
ARCH = SB + "/归档"


def ck(cond, what, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {what}")
    else:
        FAIL += 1
        print(f"  ✗ {what}   {extra!r}")


def d(path):
    return {"path": path, "name": path.rpartition("/")[2],
            "size": 0, "mtime": 0, "isdir": True}


def f(path, size=100):
    return {"path": path, "name": path.rpartition("/")[2],
            "size": size, "mtime": 0, "isdir": False}


def scene(paths):
    """[路径(,大小)] → entries（目录条目照真实扫描结果补出来）"""
    ents, seen = [], set()
    for item in paths:
        p, size = (item if isinstance(item, tuple) else (item, 100))
        parent = p.rpartition("/")[0]
        if parent and parent not in seen:
            seen.add(parent)
            ents.append(d(parent))
        ents.append(f(p, size))
    return [d(ARCH)] + ents


BASE = [f"{ARCH}/书.epub", f"{ARCH}/书(1).epub", f"{ARCH}/书(2).epub"]

print("\n【1】主名已在 · dedupe：多余的副本删掉")
ops, info = pt.build_merge_files_plan(scene(BASE), how="dedupe")
plan = {o["path"]: o for o in ops}
ck(info["fgroups"] == 1, "识别出 1 组重复文件", info["fgroups"])
ck(len(ops) == 2, "两个副本都要动", [o["path"] for o in ops])
ck(plan[f"{ARCH}/书(1).epub"]["op"] == "delete", "★ 书(1) 是删除")
ck(plan[f"{ARCH}/书(2).epub"]["op"] == "delete", "★ 书(2) 也是删除")
ck(plan[f"{ARCH}/书(1).epub"].get("size") == 100, "删的那条带着大小（导出 CSV 要用）")
ck(f"{ARCH}/书.epub" not in plan, "主名那份不动")

print("\n【2】主名已在 · move：不删，原样留着并如实报出来")
ops, info = pt.build_merge_files_plan(scene(BASE), how="move")
ck(ops == [], "move 模式下主名已有 ⇒ 一条计划都不出", [o["path"] for o in ops])
ck(info["fkept"] == 2, "★ 报出 2 个按「不删」策略原样保留", info["fkept"])

print("\n【3】★ 主名缺席、有两兄弟 ⇒ 最小的那个上位")
MISS = [f"{ARCH}/报告(1).pdf", f"{ARCH}/报告(2).pdf", f"{ARCH}/报告(3).pdf"]
ops, info = pt.build_merge_files_plan(scene(MISS), how="dedupe")
plan = {o["path"]: o for o in ops}
ck(info["fadopted"] == 1, "记为「缺主名·小号上位」1 组", info["fadopted"])
ck(plan[f"{ARCH}/报告(1).pdf"]["op"] == "rename", "★ 最小的 (1) 改回主名")
ck(plan[f"{ARCH}/报告(1).pdf"].get("newname") == "报告.pdf",
   "改成的正是主名", plan[f"{ARCH}/报告(1).pdf"].get("newname"))
ck(plan[f"{ARCH}/报告(2).pdf"]["op"] == "delete", "(2) 删掉")
ck(plan[f"{ARCH}/报告(3).pdf"]["op"] == "delete", "(3) 删掉")
ck(info["fdup_deleted"] == 2 and info["frenamed"] == 1, "统计对得上", info)

print("\n【3b】同样场面 · move：只正名，一个不删")
ops, info = pt.build_merge_files_plan(scene(MISS), how="move")
kinds = sorted((o["op"], o.get("newname", "")) for o in ops)
ck(kinds == [("rename", "报告.pdf")], "★ 只有一条改名，(2)(3) 留着", kinds)
ck(info["fkept"] == 2, "(2)(3) 报为原样保留", info["fkept"])

print("\n【4】★ 大小不一致 ⇒ 当成成套资料放行，一个不动")
DIFF = [f"{ARCH}/讲义.epub", (f"{ARCH}/讲义(1).epub", 4096),
        (f"{ARCH}/讲义(2).epub", 8192)]
ops, info = pt.build_merge_files_plan(scene(DIFF), how="dedupe")
ck(ops == [], "★ 大小不同的两个副本都放行", [o["path"] for o in ops])
ck(info["fsize_diff"] == 2, "★ 报出 2 个「大小不一致，没敢动」", info["fsize_diff"])
ck(info["fdup_deleted"] == 0, "一个都没删")

print("\n【4b】一串全是不同大小（两册资料）⇒ 连「给最小的正名」都不做")
ops, info = pt.build_merge_files_plan(scene(DIFF), how="move")
ck(ops == [], "★ 换「不删」模式也不给没证据的猜测改名", [o["path"] for o in ops])
ck(info["frenamed"] == 0 and info["fadopted"] == 0, "不算正名、不算小号当家", info)

print("\n【4c】部分大小一致：只处理能确认的那些")
MIX = [f"{ARCH}/讲义.epub", (f"{ARCH}/讲义(1).epub", 100),
       (f"{ARCH}/讲义(2).epub", 100), (f"{ARCH}/讲义(3).epub", 8192)]
ops, info = pt.build_merge_files_plan(scene(MIX), how="dedupe")
got = {o["path"]: o["op"] for o in ops}
ck(got.get(f"{ARCH}/讲义(1).epub") == "delete", "同大小的 (1) 删掉", got)
ck(got.get(f"{ARCH}/讲义(2).epub") == "delete", "同大小的 (2) 删掉", got)
ck(f"{ARCH}/讲义(3).epub" not in got, "★ 大小不同的 (3) 留着", got)
ck(info["fsize_diff"] == 1, "报出 1 个没敢动的", info["fsize_diff"])

print("\n【4d】有一边的大小查不到 ⇒ 按「不确定」放过")
ents = scene([f"{ARCH}/字典.epub", f"{ARCH}/字典(1).epub"])
for e in ents:                      # 网盘偶尔不给 size。两个 None 相等，
    e.pop("size", None)             # 直接比对会把「不知道」当成「一样」
ops, info = pt.build_merge_files_plan(ents, how="dedupe")
ck(ops == [], "★ 不知道大小 ⇒ 不许当成同一份删掉", [o["path"] for o in ops])
ck(info["fsize_diff"] == 1, "报出 1 个没敢动的", info["fsize_diff"])

print("\n【5】孤零零一个 X(1) 且没有主名 ⇒ 不许动它")
ops, info = pt.build_merge_files_plan(scene([f"{ARCH}/第三部(1).txt"]),
                                      how="dedupe")
ck(ops == [], "★ 没有旁证的单个副本不动", [o["path"] for o in ops])
ck(info["fgroups"] == 0, "也不算成重复文件组", info["fgroups"])

print("\n【6】target 路由：认不出来的参数都不能把某一层悄悄跳过")
BOTH = [f"{ARCH}/txt/c.txt", f"{ARCH}/txt(1)/c.txt",
        f"{ARCH}/txt(1)/c(1).txt",     # 这棵树里自己还有一组重复文件
        f"{ARCH}/书.epub", f"{ARCH}/书(1).epub"]
ops, info = pt.build_merge_plan(scene(BOTH), how="dedupe", target="both")
paths = [o["path"] for o in ops]
ck(len(paths) == len(set(paths)), "★ 一条路径只出现一次（同一批文件不许安排两次）",
   paths)
ck(info["pairs"] == 1, "目录层：1 对重复目录", info["pairs"])
ck(info.get("fgroups") is not None, "★ 文件层也算了（fgroups 有值）", info.get("fgroups"))
kinds = {o["path"]: o["op"] for o in ops}
ck(kinds.get(f"{ARCH}/txt(1)/c(1).txt") == "move",
   "★ 目录层安排过的那棵树，文件层绕开（它是搬回去，不是删）",
   kinds)
ck(kinds.get(f"{ARCH}/书(1).epub") == "delete",
   "根目录那组重复文件照删（没被目录层碰过）", kinds)

ops2, info2 = pt.build_merge_plan(scene(BOTH), how="dedupe", target="file")
ck(info2.get("pairs") is None, "target=file ⇒ 不带目录层统计", info2.get("pairs"))
# target=file 时 txt(1) 那棵树没人管 ⇒ 树里的 c(1).txt 也该被文件层归拢掉
k2 = {o["path"]: o["op"] for o in ops2}
ck(k2.get(f"{ARCH}/txt(1)/c(1).txt") == "delete",
   "target=file ⇒ 子目录里的重复文件照样处理", k2)
ck(k2.get(f"{ARCH}/书(1).epub") == "delete", "根目录那组也处理", k2)
ck(f"{ARCH}/txt(1)/c.txt" not in k2, "不是副本的正经文件不动", k2)

ops3, info3 = pt.build_merge_plan(scene(BOTH), how="dedupe", target="dir")
ck(info3.get("fgroups") is None, "target=dir ⇒ 不带文件层统计")
ck(f"{ARCH}/书(1).epub" not in [o["path"] for o in ops3],
   "target=dir ⇒ 夹在同一层的重复文件不动")

ops4, info4 = pt.build_merge_plan(scene(BOTH), how="dedupe", target="垃圾值")
ck([o["path"] for o in ops4] == paths,
   "★ target 认不出来 ⇒ 按 both 全做（不许因为参数脏就少做一层）")

print("\n【7】怎么删都绕开备份区")
ops, info = pt.build_merge_plan(
    scene([f"{SB}/_覆盖备份/20261009/书.epub", f"{SB}/_覆盖备份/20261009/书(1).epub"]),
    how="dedupe", target="file")
ck(ops == [], "备份区里的文件一个都不碰", [o["path"] for o in ops])

print(f"\n{'='*46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'='*46}")
sys.exit(1 if FAIL else 0)
