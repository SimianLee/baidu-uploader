# -*- coding: utf-8 -*-
"""「重名删除」策略的回归测试。

老李的要求（2026-10-09）：网盘整理里的「重名覆盖」直接改成**删除**——
撞到网盘上已有的同名文件时，把那个旧文件删掉让这次的名字上位，
并且**不生成 _覆盖备份 里的任何东西**（以前是先挪进备份区，占地方还得手动清）。

要盯住的四件事：
  一、delete 模式下走的是 delete_batch，旧文件真没了，**备份区一个目录都不建**
  二、skip 模式照旧：保留旧文件、给这次的编号 A (2)，一条都不删
  三、删不掉的不能硬来：撞名旧文件没删成功就整个中止（否则后面改名必撞 -8）
  四、并发安全红线：新版 Conflict 检测逻辑——
      · 自己的 case-only 改名（A.txt → a.txt）不算撞名，不许删自己
      · build_*_plan 里「第二个同名」不许再把第一个删掉
  五、旧预览 / 老命令行参数里的 overwrite 要等价于 delete（不能悄悄降级成 skip）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pan_tools as pt  # noqa: E402

pt.VERIFY_WAIT = 0

PASS = FAIL = 0
SB = "/apps/baidu_uploader"
SRC = SB + "/书库/a"
SRC2 = SB + "/书库/b"
DST = SB + "/归档/txt"
BK = pt.backup_root(SB)


def ck(cond, what, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {what}")
    else:
        FAIL += 1
        print(f"  ✗ {what}   {extra!r}")


class P(pt.PanFiles):
    """假网盘：delete 可按路径黑名单独失败，用来验「删不掉就中止」"""

    def __init__(self, tree, fail_del=()):
        super().__init__("tok", SB)
        self.tree = {d: dict(f) for d, f in tree.items()}
        self.fail_del = set(fail_del)
        self.deleted = []
        self.batch_log = []         # 每批提交的 op 类型，用来看清走的是 delete 还是 move

    def _get(self, url, params, retry=2):
        return {"errno": 0, "list": []}

    def _post(self, url, params, data=None, retry=2):
        import json as _json
        chunk = _json.loads(data["filelist"])
        ops = set(("delete" if "dest" not in it and "newname" not in it
                   else "move" if "dest" in it else "rename") for it in chunk)
        self.batch_log.append(sorted(ops))
        for it in chunk:
            kind = ("delete" if "dest" not in it and "newname" not in it
                    else "move" if "dest" in it else "rename")
            if kind == "delete" and it["path"] in self.fail_del:
                return {"errno": 12, "info": [{"path": it["path"], "errno": 111}
                                              for it in chunk]}
        for it in chunk:
            self._apply(it)
        return {"errno": 0, "info": []}

    def _apply(self, it):
        p = it["path"]
        d, _, n = p.rpartition("/")
        if p not in self.tree and (d not in self.tree or n not in self.tree[d]):
            return
        if "dest" in it:
            # 真网盘的 move 支持 newname（撞名一步到位），这里必须照做，
            # 否则「跳过模式下编成 A (2) 搬过去」这条永远测不出来
            del self.tree[d][n]
            self.tree.setdefault(it["dest"], {})[it.get("newname") or n] = 1
        elif "newname" in it:
            del self.tree[d][n]
            self.tree[d][it["newname"]] = 1
        else:
            if p in self.tree:
                del self.tree[p]
            elif d in self.tree and n in self.tree[d]:
                del self.tree[d][n]
            self.deleted.append(p)

    def mkdir(self, path):
        self.tree.setdefault(path, {})
        return True

    def list_dir(self, path, recursive=False, limit=1000, on_progress=None,
                 should_stop=None, failures=None):
        return [{"path": f"{path}/{n}", "name": n, "size": 10,
                 "isdir": False, "mtime": 0}
                for n in self.tree.get(path, {})]


print("【1】策略归一化：overwrite 是 delete 的旧名，不能悄悄降级成 skip")
ck(pt.dup_kind("delete") == "delete", "delete → delete")
ck(pt.dup_kind("overwrite") == "delete", "★ overwrite 等价于 delete")
ck(pt.dup_kind("OVERWRITE") == "delete", "大小写不敏感")
ck(pt.dup_kind("replace") == "delete", "replace 也当成 delete")
ck(pt.dup_kind("skip") == "skip", "skip → skip")
ck(pt.dup_kind("") == "skip", "空值按最安全的 skip")
ck(pt.dup_kind(None) == "skip", "None 按最安全的 skip")
ck(pt.dup_kind("手滑打错") == "skip", "★ 认不出来的一律按 skip（不许误删）")

print("\n【2】★ 重名删除：旧文件被真删，且不产生备份区")
tree = {SRC: {"书.txt": 1}, DST: {"书.txt": 1}}
pan = P(tree)
ops = [{"op": "move", "path": f"{SRC}/书.txt", "name": "书.txt", "dest": DST}]
st = pt.apply_plan(pan, ops, ondup="delete")
ck(pan.tree.get(DST, {}).get("书.txt") == 1, "目标处是新搬来的文件",
   pan.tree.get(DST))
ck(not pan.tree[SRC], "源目录空了", pan.tree[SRC])
ck(st["dup_deleted"] == 1, "★ 记「重名删掉 1 个」", st["dup_deleted"])
ck(st["backup"] == 0, "★ 备份数是 0（没有挪备份区）", st["backup"])
ck(not [d for d in pan.tree if d.startswith(BK)],
   "★ 备份区一个目录都没建", [d for d in pan.tree if d.startswith(BK)])
ck(["delete"] in pan.batch_log, "★ 走的是 delete 而不是 move",
   pan.batch_log)

print("\n【3】★ 改名撞名：旧文件被删掉，新名字直接上位")
tree = {SRC: {"旧名.txt": 1, "目标名.txt": 1}}
pan = P(tree)
ops = [{"op": "rename", "path": f"{SRC}/旧名.txt", "newname": "目标名.txt"}]
st = pt.apply_plan(pan, ops, ondup="delete")
ck(pan.tree[SRC].get("目标名.txt") == 1, "改名后的文件在", pan.tree[SRC])
ck(pan.tree[SRC].get("旧名.txt") is None, "旧文件名没了", pan.tree[SRC])
ck(st["rename"] == 1 and st["dup_deleted"] == 1,
   "改名 1 / 重名删 1", (st["rename"], st["dup_deleted"]))
ck(not [d for d in pan.tree if d.startswith(BK)], "没有备份区", pan.tree.keys())

print("\n【4】skip 模式照旧：一个都不删，后来的编号")
tree = {SRC: {"书.txt": 1}, DST: {"书.txt": 1}}
pan = P(tree)
ops = [{"op": "move", "path": f"{SRC}/书.txt", "name": "书.txt", "dest": DST,
        "newname": "书 (2).txt"}]
st = pt.apply_plan(pan, ops, ondup="skip")
ck(pan.tree.get(DST, {}).get("书.txt") == 1, "★ 网盘上旧的那个还在",
   pan.tree.get(DST))
ck(pan.tree.get(DST, {}).get("书 (2).txt") == 1, "这次的编成 书 (2).txt 进来了",
   pan.tree.get(DST))
ck(st["dup_deleted"] == 0, "一条都没删", st["dup_deleted"])
ck(not pan.deleted, "delete_batch 压根没被调用", pan.deleted)

print("\n【5】★ 删不掉就中止：绝不带着没删干净的旧文件硬改名")
tree = {SRC: {"书.txt": 1}, DST: {"书.txt": 1}}
pan = P(tree, fail_del=[f"{DST}/书.txt"])
ops = [{"op": "move", "path": f"{SRC}/书.txt", "name": "书.txt", "dest": DST}]
st = pt.apply_plan(pan, ops, ondup="delete")
ck(pan.tree.get(DST, {}).get("书.txt") == 1, "旧的没删掉，还在", pan.tree.get(DST))
ck(pan.tree.get(SRC, {}).get("书.txt") == 1, "★ 源文件也还在（没被稀里糊涂搬过去）",
   pan.tree.get(SRC))
ck(st["move"] == 0 and st["dup_deleted"] == 0, "★ 后续操作没有执行",
   (st["move"], st["dup_deleted"]))
ck(any("中止" in (f.get("msg") or "") for f in st["fails"]),
   "失败清单写明已中止", [f.get("msg") for f in st["fails"]])

print("\n【6】★ 只改大小写不算撞名：不许把自己删了")
tree = {SRC: {"A.txt": 1}}
pan = P(tree)
ops = [{"op": "rename", "path": f"{SRC}/A.txt", "newname": "a.txt"}]
st = pt.apply_plan(pan, ops, ondup="delete")
ck(st["dup_deleted"] == 0, "★ 没有把它当成重名旧文件删掉", st["dup_deleted"])
ck(not pan.deleted, "没有触发任何 delete", pan.deleted)
ck(pan.tree[SRC].get("a.txt") == 1, "改名成功", pan.tree[SRC])

print("\n【7】计划层：第二个同名不许再把第一个删掉（数据安全红线）")
files = [{"path": f"{SRC}/书.txt", "name": "书.txt", "size": 1, "mtime": 0},
         {"path": f"{SRC2}/书.txt", "name": "书.txt", "size": 1, "mtime": 0}]
occ = {DST: {"书.txt"}}
ops = pt.build_organize_plan(files, "ext", SB + "/归档", SB, occ, ondup="delete")
by_src = {o["path"]: o.get("newname") or o["name"] for o in ops}
ck(by_src.get(f"{SRC}/书.txt") == "书.txt",
   "★ 头一个原样搬进去（会删掉网盘上旧的那个）", by_src)
ck(by_src.get(f"{SRC2}/书.txt") == "书 (2).txt",
   "★ 第二个编成 书 (2).txt —— 不是又一次删除", by_src)
ck(sum(1 for o in ops if o.get("overwrite")) == 1, "只有一条标了待删除",
   [o.get("overwrite") for o in ops])

print("\n【8】合并重复：主目录里已有的同名，delete 下也不编号")
ents = [
    {"path": f"{SB}/归档/txt", "name": "txt", "isdir": True, "size": 0},
    {"path": f"{SB}/归档/txt(1)", "name": "txt(1)", "isdir": True, "size": 0},
    {"path": f"{SB}/归档/txt/书.txt", "name": "书.txt", "isdir": False, "size": 1},
    {"path": f"{SB}/归档/txt(1)/书.txt", "name": "书.txt", "isdir": False, "size": 1},
]
ops, info = pt.build_merge_dirs_plan(ents, ondup="delete")
ck(ops and ops[0].get("newname") is None, "★ 不编号，原样合回去", ops)
ck(info["overwrite"] == 1, "info 报出 1 条要删旧的", info)
ops2, info2 = pt.build_merge_dirs_plan(ents, ondup="skip")
ck(ops2[0].get("newname") == "书 (2).txt", "skip 模式下照样编号", ops2)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
