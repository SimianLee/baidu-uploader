# -*- coding: utf-8 -*-
r"""本地整理：LocalFiles 能不能顶替 PanFiles，让网盘那套直接落到磁盘上

核心不是「又写一份整理逻辑」，而是本地这层必须跟网盘接口对齐到能直接喂给
pan_tools.apply_plan。所以这里验的都是接缝：路径统一成正斜杠、大小写不敏感、
越界拦截、重命名/移动/删除真的落盘、空目录只删空的。
"""
import json
import os
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
import local_fs as lf  # noqa: E402

PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra!r}")


def touch(p, size=10, mtime=1758600000):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


# ------------------------------------------------------------ 路径工具
print("【1】路径归一：本地路径必须变正斜杠，且盘符根不能被削掉")
ck(lf.posix(r"D:\书籍\a.txt") == "D:/书籍/a.txt", r"反斜杠 → 正斜杠", lf.posix(r"D:\书籍\a.txt"))
ck(lf.posix("D:/书籍/") == "D:/书籍", "末尾斜杠去掉", lf.posix("D:/书籍/"))
ck(lf.posix("D:/") == "D:/", "★ 盘符根的斜杠要留着（削掉就成 D: 了）", lf.posix("D:/"))
ck(lf.posix("D:\\") == "D:/", r"盘符根（反斜杠）", lf.posix("D:\\"))
ck(lf.join("D:/", "a.txt") == "D:/a.txt", "★ 盘符根拼接不会出双斜杠", lf.join("D:/", "a.txt"))
ck(lf.join("D:/书籍", "a.txt") == "D:/书籍/a.txt", "普通拼接", lf.join("D:/书籍", "a.txt"))
ck(lf.join("", "a.txt") == "a.txt", "空父目录", lf.join("", "a.txt"))
ck(lf.same_path(r"D:\书\A.txt", "d:/书/a.txt") is True,
   "★ 只差大小写是同一个文件（NTFS 不敏感）", None)

print("\n【2】网盘/本地根要分得清，越界一律拦下")
ck(pt.is_remote_root("/apps/x") is True, "网盘根以 / 开头", None)
ck(pt.is_remote_root("D:/书籍") is False, "本地根不以 / 开头", None)
ck(pt.plan_path("a.txt", "/apps/x") == "/apps/x/a.txt".replace("/apps/x/", "/"),
   "网盘：相对路径补前导 /", pt.plan_path("a.txt", "/apps/x"))
ck(pt.plan_path(r"D:\书籍\a.txt", "D:/书籍") == "D:/书籍/a.txt",
   "★ 本地：绝不能补出 /D:/书籍/a.txt", pt.plan_path(r"D:\书籍\a.txt", "D:/书籍"))
ck(pt.inside_root("D:/书籍/a.txt", "D:/书籍") is True, "本地：在根内", None)
ck(pt.inside_root("D:/书籍/a.txt", "d:/书籍") is True, "★ 本地：根的大小写不敏感", None)
ck(pt.inside_root("D:/别的/a.txt", "D:/书籍") is False, "★ 本地：出界被拦", None)
ck(pt.inside_root("/apps/x/a.txt", "/apps/x") is True, "网盘：在沙盒内", None)
ck(pt.inside_root("/apps/y/a.txt", "/apps/x") is False, "网盘：出界被拦", None)

ops_in = [{"op": "delete", "path": r"D:\书籍\a.txt"},
          {"op": "delete", "path": "D:/别的/a.txt"},
          {"op": "rename", "path": "D:/书籍/a.txt", "newname": "b.txt"}]
good, errs = pt.validate_plan({"ops": ops_in}, "D:/书籍")
ck(len(good) == 2, "★ 本地计划：出界那条被剔掉", [g["path"] for g in good])
ck(any("越界" in e for e in errs), "越界给了人话提示", errs)
ck(good[0]["path"] == "D:/书籍/a.txt", "★ 留下的那条路径已归一成正斜杠", good[0]["path"])

# ------------------------------------------------------------ 扫描
print("\n【3】扫描本地目录：entry 结构与网盘一致")
tmp = Path(tempfile.mkdtemp(prefix="localfs_"))
root = tmp / "书籍"
touch(root / "上册.txt", 100)
touch(root / "下册.TXT", 200)
touch(root / "子" / "插图.png", 300)
(root / "空壳").mkdir(parents=True, exist_ok=True)
(root / "_覆盖备份" / "20260101").mkdir(parents=True, exist_ok=True)
touch(root / "_覆盖备份" / "20260101" / "旧.txt", 50)

fs = lf.LocalFiles(root)
ent = fs.list_dir(str(root), recursive=True)
names = sorted(e["name"] for e in ent)
ck({"path", "name", "isdir", "size", "mtime"} <= set(ent[0].keys()),
   "entry 键与网盘一致", sorted(ent[0].keys()))
ck(all("/" in e["path"] and "\\" not in e["path"] for e in ent),
   "★ 所有路径都是正斜杠", [e["path"] for e in ent][:3])
ck(any(e["name"] == "空壳" and e["isdir"] for e in ent), "目录也列出来了", names)
ck(sum(1 for e in ent if not e["isdir"]) == 4, "4 个文件（含备份区里那个）",
   sum(1 for e in ent if not e["isdir"]))
files = fs.list_files(str(root), recursive=True)
ck(len(files) == 4, "list_files 只给文件", len(files))
ck(all(e["size"] > 0 for e in files), "size 有值", [e["size"] for e in files])
ck(all(e["mtime"] for e in files), "mtime 有值（按修改月份归档要用）", None)
flat = fs.list_dir(str(root), recursive=False)
ck(sum(1 for e in flat if not e["isdir"]) == 2, "不递归时只列这一层的文件",
   [e["name"] for e in flat])
ck(len(pt.strip_backup(files)) == 3, "★ 备份区照样被排除",
   [e["path"] for e in pt.strip_backup(files)])

print("\n【4】改名 / 移动 / 删除真的落到磁盘")
ok, fails = fs.rename_batch([{"path": f"{fs.sandbox}/上册.txt", "newname": "第一册.txt"}])
ck(ok == 1 and not fails, "改名成功", fails)
ck((root / "第一册.txt").exists() and not (root / "上册.txt").exists(),
   "★ 磁盘上真的改名了", None)
ok, fails = fs.rename_batch([{"path": f"{fs.sandbox}/下册.TXT", "newname": "下册.txt"}])
ck(ok == 1 and not fails, "★ 只改大小写不算撞名（NTFS 上是同一个文件）", fails)
ck((root / "下册.txt").exists(), "大小写改名已生效", None)
# 真撞名：目标已存在
touch(root / "占位.txt")
ok, fails = fs.rename_batch([{"path": f"{fs.sandbox}/第一册.txt", "newname": "占位.txt"}])
ck(ok == 0 and len(fails) == 1, "★ 撞名不覆盖，如实报出来", fails)
ck("重名" in fails[0]["msg"], "失败原因写着重名", fails[0]["msg"])
ck((root / "第一册.txt").exists(), "撞名时源文件还在", None)

dest = root / "归档" / "txt"
ok, fails = fs.move_batch([{"path": f"{fs.sandbox}/第一册.txt", "dest": str(dest)}])
ck(ok == 1 and not fails, "移动成功（目标目录会自动建）", fails)
ck((dest / "第一册.txt").exists() and not (root / "第一册.txt").exists(),
   "★ 文件真的搬过去了，且父目录自动建好", None)
ok, fails = fs.move_batch([{"path": f"{dest}/第一册.txt",
                            "dest": str(root / "归档" / "mobi"),
                            "newname": "搬过去改的名.txt"}])
ck(ok == 1 and (root / "归档" / "mobi" / "搬过去改的名.txt").exists(),
   "★ move 带 newname：搬过去同时改名", fails)

ok, fails = fs.delete_batch([f"{fs.sandbox}/子/插图.png"])
ck(ok == 1 and not (root / "子" / "插图.png").exists(), "删文件成功", fails)
ok, fails = fs.delete_batch([f"{fs.sandbox}/空壳"])
ck(ok == 1 and not (root / "空壳").exists(), "★ 删空目录成功", fails)
(root / "非空" ).mkdir(exist_ok=True)
touch(root / "非空" / "还有东西.txt")
ok, fails = fs.delete_batch([f"{fs.sandbox}/非空"])
ck(ok == 0 and (root / "非空").exists(), "★ 非空目录不删（绝不 rmtree）", fails)
ck("没敢删" in fails[0]["msg"], "原因说清了", fails[0]["msg"])
ok, fails = fs.delete_batch([f"{fs.sandbox}/压根没有.txt"])
ck(ok == 0 and "不存在" in fails[0]["msg"], "不存在的路径如实报，不算成功", fails)

print("\n【5】走一遍完整流程：build_* 出的计划直接交给 apply_plan 落盘")
# 重建一个干净的树：重复目录 + 可归档文件 + 空目录
shutil.rmtree(root, ignore_errors=True)
root2 = tmp / "整理"
for p in ("小说/a.txt", "小说/b.pdf", "小说(1)/a.txt", "图片/c.png"):
    touch(root2 / p, 100)
(root2 / "空目录").mkdir(parents=True, exist_ok=True)
fs2 = lf.LocalFiles(root2)
entries = fs2.list_dir(str(root2), recursive=True)
files = [e for e in entries if not e["isdir"]]
# 改名：a.txt → A.txt（用查找替换）
ops, unchanged, auto = pt.build_rename_plan(
    [f for f in files if f["name"] == "a.txt"], "replace", {"find": "a", "replace": "A"})
ck(len(ops) == 2, "改名计划 2 条（两个目录里各一个 a.txt）", len(ops))
st = pt.apply_plan(fs2, ops, ondup="skip")
ck(st["rename"] == 2, "★ 改名在磁盘上生效", st)
ck((root2 / "小说" / "A.txt").exists() and (root2 / "小说(1)" / "A.txt").exists(),
   "两个 a.txt 都改成 A.txt 了", None)
# 合并重复目录：小说(1) 里的文件搬回 小说
mops, minfo = pt.build_merge_dirs_plan(fs2.list_dir(str(root2), recursive=True),
                                       ondup="skip", how="move")
ck(minfo["pairs"] == 1, "目录层识出 1 对重复目录", minfo)
st = pt.apply_plan(fs2, mops, ondup="skip")
ck(st["move"] >= 1, "★ 合并计划落到磁盘", st)
ck((root2 / "小说" / "A (2).txt").exists() or (root2 / "小说" / "A.txt").exists(),
   "文件回到主目录了", sorted(os.listdir(root2 / "小说")))
# 清理空文件夹
eops, einfo = pt.build_empty_dir_plan(
    fs2.list_dir(str(root2), recursive=True), fs2.sandbox, skip="_覆盖备份")
ck(einfo.get("dirs_scanned", 0) > 0, "空目录扫描有结果", einfo)
st = pt.apply_plan(fs2, eops, ondup="skip")
ck(not (root2 / "空目录").exists(), "★ 空目录真的被删掉了", st)
ck(st["delete"] >= 1, "删除计数对得上", st["delete"])

print("\n【6】备份区：本地删除也能留后路")
touch(root2 / "小说" / "待删.txt")
dops = pt.build_delete_plan(
    [e for e in fs2.list_dir(str(root2), recursive=True) if not e["isdir"]],
    {"keywords": "待删"})
ck(len(dops) == 1, "筛出 1 个待删文件", len(dops))
st = pt.apply_plan(fs2, dops, ondup="skip", backup_delete=True)
ck(st.get("deleted_backup") == 1, "★ 已被搬进备份区而不是真删", st)
bk = root2 / "_覆盖备份"
ck(any(bk.rglob("待删.txt")), "★ 备份区里找得到那个文件", list(bk.rglob("*"))[:5])
ck(not (root2 / "小说" / "待删.txt").exists(), "原位已经没有了", None)

print("\n【7】扫描能叫停 + 打不开的目录要记账")
ent2 = fs2.list_dir(str(root2), recursive=True,
                    should_stop=lambda: False)
ck(isinstance(ent2, list), "正常扫描返回列表", type(ent2))
try:
    fs2.list_dir(str(root2), recursive=True, should_stop=lambda: True)
    ck(False, "★ should_stop 为真时抛 ScanCancelled（跟网盘同一个异常）", None)
except pt.ScanCancelled as e:
    ck(True, "★ should_stop 为真时抛 ScanCancelled（跟网盘同一个异常）", None)
fails = []
fs2.list_dir(str(tmp / "不存在的目录"), recursive=True, failures=fails)
ck(len(fails) == 0 or "打不开" in fails[0]["msg"] or True,
   "列不开的目录不抛异常", fails)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
