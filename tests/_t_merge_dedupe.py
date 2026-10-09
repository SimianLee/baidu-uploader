# -*- coding: utf-8 -*-
"""合并重复目录：两种处理方式 + 主目录缺席时的「小号当家」

【1】how=move（默认）：副本里的文件全搬回主目录，撞名按重名策略编号/删旧
【2】how=dedupe：主目录里已经有同名文件的，副本那份**直接删掉**；
                副本里主目录没有的（独有文件）照常搬回去——去重不是清空
【3】主目录不在时（只有 txt(1) txt(2) txt(3)，没有 txt），序号最小的那个当家，
    剩下的合进去。不做这一步，用户看到的那一串副本永远消不掉
【4】安全红线：副本里的子目录不动、副本目录本身不在这份计划里删
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


def f(path):
    return {"path": path, "name": path.rpartition("/")[2],
            "size": 10, "mtime": 0, "isdir": False}


def scene(files):
    """[路径] → entries 列表（目录条目跟着补出来，跟真实扫描结果一致）"""
    ents, seen = [], set()
    for p in files:
        parent = p.rpartition("/")[0]
        if parent and parent not in seen:
            seen.add(parent)
            ents.append(d(parent))
        ents.append(f(p))
    return [d(ARCH)] + ents


FILES = [f"{ARCH}/txt/a.txt", f"{ARCH}/txt/b.txt",
         f"{ARCH}/txt(1)/a.txt", f"{ARCH}/txt(1)/c.txt"]


print("\n【1】how=move：副本里的文件全搬回主目录，撞名的编号")
ops, info = pt.build_merge_dirs_plan(scene(FILES), how="move")
plan = {o["path"]: o for o in ops}
ck(info["pairs"] == 1, "识别出 1 对重复目录", info["pairs"])
ck(len(ops) == 2, "副本里两个文件都要动", len(ops))
ck(plan[f"{ARCH}/txt(1)/a.txt"]["op"] == "move", "同名的 a.txt 是移动（+编号）")
ck(plan[f"{ARCH}/txt(1)/a.txt"].get("newname") == "a (2).txt",
   "★ 撞到主目录已有的 a.txt → 编成 a (2).txt",
   plan[f"{ARCH}/txt(1)/a.txt"].get("newname"))
ck(plan[f"{ARCH}/txt(1)/c.txt"].get("newname") is None,
   "主目录没有的 c.txt 原名搬进去", plan[f"{ARCH}/txt(1)/c.txt"])
ck(info["dup_deleted"] == 0, "move 模式不删任何文件", info["dup_deleted"])

print("\n【2】★ how=dedupe：同名那份删掉，独有文件仍搬回")
ops, info = pt.build_merge_dirs_plan(scene(FILES), how="dedupe")
plan = {o["path"]: o for o in ops}
ck(info["pairs"] == 1, "还是 1 对", info["pairs"])
ck(plan[f"{ARCH}/txt(1)/a.txt"]["op"] == "delete",
   "★ 主目录已有 a.txt → 副本这份删掉", plan[f"{ARCH}/txt(1)/a.txt"])
ck(plan[f"{ARCH}/txt(1)/c.txt"]["op"] == "move",
   "★ 主目录没有的 c.txt 照样搬回去（去重不是清空）",
   plan[f"{ARCH}/txt(1)/c.txt"])
ck(info["dup_deleted"] == 1 and info["moved"] == 1,
   "统计分开：删 1 / 搬 1", (info["dup_deleted"], info["moved"]))
ck(info["how"] == "dedupe", "info 里带回 how（前端据此措辞）", info["how"])
ck(not any(o.get("isdir") for o in ops), "★ 一条删目录的都没有", ops)

print("\n【3】两个副本撞同一个 → 第一个搬进去，第二个算重复删掉")
files3 = [f"{ARCH}/txt/a.txt", f"{ARCH}/txt(1)/z.txt", f"{ARCH}/txt(2)/z.txt"]
ops, info = pt.build_merge_dirs_plan(scene(files3), how="dedupe")
plan = {o["path"]: o for o in ops}
ck(plan[f"{ARCH}/txt(1)/z.txt"]["op"] == "move", "头一个 z.txt 搬进去（还没人占）")
ck(plan[f"{ARCH}/txt(2)/z.txt"]["op"] == "delete",
   "★ 第二个 z.txt 是重复（第一个刚占了这个名字）→ 删掉",
   plan[f"{ARCH}/txt(2)/z.txt"])
ck(info["pairs"] == 2, "两个副本各算一对", info["pairs"])

print("\n【4】★ 主目录缺席：txt(1) txt(2) txt(3)，让序号最小的当家")
files4 = [f"{ARCH}/txt(1)/a.txt", f"{ARCH}/txt(2)/a.txt", f"{ARCH}/txt(2)/b.txt",
          f"{ARCH}/txt(3)/a.txt"]
ops, info = pt.build_merge_dirs_plan(scene(files4), how="dedupe")
plan = {o["path"]: o for o in ops}
ck(info["adopted"] == 1, "报出来有 1 组缺主目录", info["adopted"])
ck(info["pairs"] == 2, "txt(2) txt(3) 各自合向 txt(1)", info["pairs"])
ck(f"{ARCH}/txt(1)/a.txt" not in plan, "★ 当家的 txt(1) 自己没被动")
ck(plan[f"{ARCH}/txt(2)/a.txt"]["op"] == "delete",
   "txt(2) 的 a.txt 与当家的重复 → 删掉")
ck(plan[f"{ARCH}/txt(2)/b.txt"]["op"] == "move",
   "txt(2) 独有的 b.txt 搬进当家", plan[f"{ARCH}/txt(2)/b.txt"])
ck(plan[f"{ARCH}/txt(3)/a.txt"]["op"] == "delete", "txt(3) 的也是重复 → 删掉")

print("\n【5】move 模式下同样认缺席主目录")
ops, info = pt.build_merge_dirs_plan(scene(files4), how="move")
plan = {o["path"]: o for o in ops}
ck(info["adopted"] == 1, "也报 1 组缺席", info["adopted"])
ck(plan[f"{ARCH}/txt(2)/a.txt"].get("newname") == "a (2).txt",
   "move 模式下改成编号而不是删",
   plan[f"{ARCH}/txt(2)/a.txt"].get("newname"))

print("\n【6】how 参数脏值 → 按最安全的 move 处理（绝不偷偷删文件）")
for bad in ("", None, "MOVE", "删", "overwrite", 123, {}):
    ops, info = pt.build_merge_dirs_plan(scene(FILES), how=bad)
    if any(o["op"] == "delete" for o in ops):
        FAIL += 1
        print(f"  ✗ 脏值 {bad!r} 被当成 dedupe 了")
        break
else:
    PASS += 1
    print("  ✓ 认不出来的 how 一律按 move（不删文件）")

print("\n【7】★ 副本里的子目录不动、副本目录本身不删")
files7 = [f"{ARCH}/txt(1)/x.txt", f"{ARCH}/txt(1)/子目录/deep.txt",
          f"{ARCH}/txt/a.txt"]
ents = scene(files7)
ents.append(d(f"{ARCH}/txt(1)/子目录"))
ops, info = pt.build_merge_dirs_plan(ents, how="dedupe")
paths = {o["path"] for o in ops}
ck(f"{ARCH}/txt(1)/子目录" not in paths, "★ 子目录没被搬也没被删", paths)
ck(not any(o.get("isdir") for o in ops), "没有任何目录级操作")
ck(info["with_subdirs"] == 1, "报出来「1 个副本里还有子目录」",
   info["with_subdirs"])
ck(f"{ARCH}/txt(1)" not in paths, "★ 副本目录本体不在这份计划里（防止连根拔）")

print("\n【8】备份区里的副本目录不参与合并")
bak = SB + "/_覆盖备份/20261009-120000"
files8 = [f"{bak}/归档/txt/a.txt", f"{bak}/归档/txt(1)/a.txt"]
ops, info = pt.build_merge_dirs_plan(scene(files8), how="dedupe")
ck(ops == [], "★ 备份区里的一对不生成任何操作", ops)
ck(info["pairs"] == 0, "pairs 为 0", info["pairs"])

print("\n【9】只有主目录没有副本 → 什么都不做")
ops, info = pt.build_merge_dirs_plan(scene([f"{ARCH}/txt/a.txt"]), how="dedupe")
ck(ops == [] and info["pairs"] == 0, "孤零零一个目录不成对", info)

print("\n【10】两位数字后缀不算副本（报告(2024) 是正经名字）")
files10 = [f"{ARCH}/报告/a.txt", f"{ARCH}/报告(2024)/a.txt"]
ops, info = pt.build_merge_dirs_plan(scene(files10), how="dedupe")
# 「报告(2024)」会被解析成 base=「报告(20」+序号4？不会：正则要求结尾是单个数字
ck(info["pairs"] == 0, "★ 括号里是多位数字时不配对", info["pairs"])

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
