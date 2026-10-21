# -*- coding: utf-8 -*-
r"""本地删除撞上「只读文件」：[WinError 5] 拒绝访问

实测场景：整理归档时目标位已有同名旧文件（重名策略=删旧让位），删到一批带
FILE_ATTRIBUTE_READONLY 的文件就被卡住——`os.remove` 对只读文件直接甩
[WinError 5]「拒绝访问」，而这句跟「没权限」「被别的程序占着」长得一模一样，
用户对着它压根想不出该干什么（实测 23 万个文件里撞到 2811 个只读的）。

这一套守住四件事：

1. 只读文件**要能删掉**（先摘只读再删），不能让整批卡在「为避免漏改已中止」；
2. 摘了仍失败（多半被占用）→ 把只读属性**恢复回去**，不白改人家的文件；
3. 报错说人话：占用是占用、只读是只读，别都甩一句拒绝访问；
4. 摘过只读这件事要计数报给用户（动了人家的文件属性，不能悄悄干）。
"""
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import local_fs as lf  # noqa: E402
import pan_tools as pt  # noqa: E402

PASS = FAIL = 0


def ck(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra!r}")


tmp = Path(tempfile.mkdtemp(prefix="readonly_"))


def mk(p, data="x"):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8"))
    return p


def set_ro(p, on=True):
    os.chmod(str(p), stat.S_IREAD if on else (stat.S_IREAD | stat.S_IWRITE))


try:
    root = tmp / "书"

    print("【1】先把坑钉住：裸删只读文件果然是 WinError 5")
    f = mk(root / "归档" / "txt" / "旧.txt", "旧版")
    set_ro(f)
    ck(lf.is_readonly(str(f)), "文件确实带只读属性", hex(lf.file_attrs(str(f))))
    try:
        os.remove(str(f))
        ck(False, "★ 裸删只读文件失败（前提：os.remove 会拒）", "竟然删掉了")
    except OSError as e:
        ck(getattr(e, "winerror", None) == 5,
           "★ 裸删只读文件果然甩 [WinError 5] 拒绝访问", e)

    print("\n【2】★ 走删除接口：摘掉只读后删得掉，且把这件事记下来")
    fs = lf.LocalFiles(str(root))
    ok, fails = fs.delete_batch([str(f)])
    ck(ok == 1 and not fails, "★ 只读文件删掉了（不是整批被卡住）", fails)
    ck(not f.exists(), "文件真的从磁盘上没了", None)
    ck(fs.readonly_cleared == 1,
       "★ 去掉只读这件事被记下来了（动了文件属性，得如实告诉用户）",
       fs.readonly_cleared)

    print("\n【3】普通文件照常删，不碰它的属性")
    n = mk(root / "归档" / "txt" / "普通.txt")
    ok, fails = fs.delete_batch([str(n)])
    ck(ok == 1 and not n.exists(), "普通文件正常删掉", fails)

    print("\n【4】★ 归档重名：只读的旧文件挡道，重名删旧要能搬进去")
    mk(root / "小说" / "a.txt", "新版")
    old = mk(root / "归档" / "txt" / "a.txt", "旧版")
    set_ro(old)
    fs2 = lf.LocalFiles(str(root))
    ents = fs2.list_dir(str(root), recursive=True)
    files = [e for e in ents if not e["isdir"] and "/小说/" in lf.posix(e["path"])]
    ops = pt.build_organize_plan(files, "ext", str(root / "归档"), str(root))
    st = pt.apply_plan(fs2, ops, ondup="delete")
    dst = root / "归档" / "txt" / "a.txt"
    ck(dst.exists() and dst.read_text() == "新版",
       "★ 新版真的搬进去了（只读的旧文件让了位）",
       dst.read_text() if dst.exists() else "目标不存在")
    ck(not (root / "小说" / "a.txt").exists(), "源文件搬走了", None)
    ck(not st.get("fails"), "执行没有失败项（不会被「为避免漏改已中止」卡住）",
       st.get("fails"))
    ck(getattr(fs2, "readonly_cleared", 0) >= 1,
       "★ 顺手去掉的只读属性有计数", getattr(fs2, "readonly_cleared", 0))

    print("\n【5】★ 被别的程序占着：删不掉时只读属性要恢复原样")
    b = mk(root / "占用" / "占着.txt")
    set_ro(b)
    h = open(str(b), "r")           # 占住不放，模拟看图/编辑软件开着
    try:
        ok, fails = fs.delete_batch([str(b)])
        ck(ok == 0 and fails, "★ 被占用就是删不掉（绝不硬来）", ok)
        msg = (fails[0]["msg"] if fails else "")
        ck("正被别的程序打开" in msg,
           "★ 报错说清是「被别的程序打开」，而不是笼统的拒绝访问", msg)
        ck(lf.is_readonly(str(b)),
           "★ 删不掉时只读属性恢复了（没白改人家的文件）",
           hex(lf.file_attrs(str(b))))
    finally:
        h.close()

    print("\n【6】报错分得清：占用 / 拒绝访问 / 其他")
    e32 = PermissionError(13, "x"); e32.winerror = 32
    e5 = PermissionError(13, "x"); e5.winerror = 5
    e2 = FileNotFoundError(2, "x"); e2.winerror = 2
    ck("正被别的程序打开" in lf.del_fail_msg("p", e32),
       "WinError 32 → 说清被占用", lf.del_fail_msg("p", e32))
    ck("拒绝访问" in lf.del_fail_msg("p", e5) and "只读" in lf.del_fail_msg("p", e5),
       "WinError 5 → 提到只读与被占用两种可能", lf.del_fail_msg("p", e5))
    ck(lf.del_fail_msg("p", e2).startswith("删除失败"),
       "其他错误 → 原样报出来", lf.del_fail_msg("p", e2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
