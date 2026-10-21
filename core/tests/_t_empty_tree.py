# -*- coding: utf-8 -*-
r"""空文件夹套空文件夹：本地侧必须能一层层删干净

报的是这么个现象：本地整理 → 清理空文件夹 → 「目录里还有 N 项，没敢删」，
一个都没删掉。根子在于「计划」和「本地删除」的口径对不上：

  · 空文件夹计划长期只列**最外层**（roots）：删掉父目录，嵌在里面的跟着消失。
    网盘的 delete 接口就是这么级联删的，没毛病。
  · 本地的删除是 `os.rmdir`，它要求目录**当下真的是空的**。只给最外层那条时，
    A 里还站着 B、C ⇒ rmdir(A) 必然失败，套了几层就一层都删不掉。

所以本地改成两条：计划侧 expand=True 逐层列出（自下而上排好），执行侧
delete_batch 再把**本批内的深层排在前面**先删。另外给「旧预览里只有外层那条」
兜了 _rmdir_tree——但那条路径的住手条件从严到骨子里：子树里出现一个文件、
一个联接、一层读不开，立刻收手，绝不 rmtree。

这组用例守的就是这条线：**能删的一个不落，不能删的一个不动**。
"""
import os
import shutil
import subprocess
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


def mk(p):
    Path(p).mkdir(parents=True, exist_ok=True)


def touch(p, size=10):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_bytes(b"x" * size)


tmp = Path(tempfile.mkdtemp(prefix="emptytree_"))


def fresh(name):
    """每次用一棵干净的子树，免得前后用例互相污染"""
    d = tmp / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


def plan_local(root, skip=pt.BACKUP_DIR_NAME):
    fs = lf.LocalFiles(root)
    ent = fs.list_dir(str(root), recursive=True)
    return fs, pt.build_empty_dir_plan(ent, str(root), skip=skip, expand=True)


try:
    # ------------------------------------------------------------ 报的那个现象
    print("【1】★ 空文件夹套空文件夹：一层层删干净（本地口径）")
    root = fresh("t1")
    for d in ("A/B/C", "A/D", "E"):
        mk(root / d)
    fs, (ops, info) = plan_local(root)
    rel = lambda p: lf.posix(p).replace(lf.posix(root), "")
    got = [rel(o["path"]) for o in ops]
    ck(len(ops) == 5, "★ 5 层嵌套全列出来了（不是只有最外层 A）", got)
    ck(got.index("/A/B/C") < got.index("/A/B") < got.index("/A"),
       "★ 顺序是自下而上：轮到父目录时子目录已经没了", got)
    st = pt.apply_plan(fs, ops, ondup="skip")
    ck(st["delete"] == 5 and not st["fails"], "★ 5 条全部删成功", st["fails"])
    ck(not (root / "A").exists() and not (root / "E").exists(),
       "★ 磁盘上 A（含 B/C/D）和 E 都没了", sorted(os.listdir(root)))

    print("\n【2】网盘口径不变：默认仍只给最外层（靠接口级联删）")
    root = fresh("t2")
    for d in ("A/B/C", "A/D"):
        mk(root / d)
    fs2 = lf.LocalFiles(root)
    ent = fs2.list_dir(str(root), recursive=True)
    rel2 = lambda p: lf.posix(p).replace(lf.posix(root), "")
    ops2, info2 = pt.build_empty_dir_plan(ent, str(root))
    ck([rel2(o["path"]) for o in ops2] == ["/A"],
       "★ 默认（expand=False）仍然只列最外层",
       [rel2(o["path"]) for o in ops2])
    ck(info2["nested"] == 3 and info2["empty_total"] == 4,
       "统计照旧：嵌套 3 / 空目录共 4", info2)

    print("\n【3】有文件的子树：它自己和它的祖先一个都不许动")
    root = fresh("t3")
    for d in ("有书/第1册", "有书/第2册空壳", "干净的空/子"):
        mk(root / d)
    touch(root / "有书" / "第1册" / "a.txt")
    fs, (ops, info) = plan_local(root)
    gone = {rel(o["path"]) for o in ops}
    ck("/有书" not in gone and "/有书/第1册" not in gone,
       "★ 存了文件的那一支整条不进清单", sorted(gone))
    ck("/有书/第2册空壳" in gone, "同一支里真的空的那层照样删", sorted(gone))
    st = pt.apply_plan(fs, ops, ondup="skip")
    ck(not st["fails"] and (root / "有书" / "第1册" / "a.txt").exists(),
       "★ 文件没丢", st["fails"])
    ck((root / "有书" / "第2册空壳").exists() is False, "空壳层删掉了", None)

    print("\n【4】备份区里的空目录：连同它的祖先一起保护")
    root = fresh("t4")
    mk(root / "只有备份的孩子" / pt.BACKUP_DIR_NAME / "20260101")
    mk(root / "普通的空")
    fs, (ops, info) = plan_local(root)
    gone = {rel(o["path"]) for o in ops}
    ck(f"/只有备份的孩子/{pt.BACKUP_DIR_NAME}" not in gone,
       "★ 备份区自己不删", sorted(gone))
    ck("/只有备份的孩子" not in gone,
       "★ 只装着备份区的父目录也不删（删了会把备份带走）", sorted(gone))
    ck("/普通的空" in gone, "别的空目录照删", sorted(gone))
    st = pt.apply_plan(fs, ops, ondup="skip")
    ck((root / "只有备份的孩子" / pt.BACKUP_DIR_NAME).exists(),
       "★ 执行之后备份区还在", None)

    print("\n【5】★ 旧预览里只有外层那一条：执行侧兜底也能删干净")
    root = fresh("t5")
    for d in ("A/B/C", "A/D"):
        mk(root / d)
    fs = lf.LocalFiles(root)
    ok, fails = fs.delete_batch([str(root / "A")])     # 模拟只有外层的旧清单
    ck(ok == 1 and not fails, "★ 只给外层也能删干净（子树确实没文件）", fails)
    ck(not (root / "A").exists(), "A 整棵子树没了", sorted(os.listdir(root)))

    print("\n【6】铁律：里面有文件的目录，一条都不许动")
    root = fresh("t6")
    mk(root / "非空" / "子")
    touch(root / "非空" / "子" / "真书.txt")
    fs = lf.LocalFiles(root)
    ok, fails = fs.delete_batch([str(root / "非空")])
    ck(ok == 0 and len(fails) == 1, "★ 非空目录拒绝删除", fails)
    ck("没敢删" in fails[0]["msg"], "理由写明里面还有东西", fails[0]["msg"])
    ck((root / "非空" / "子" / "真书.txt").exists(), "★ 文件好好地在那儿", None)
    # 连它的子孙也一起问一遍：谁都不能被删
    ok, fails = fs.delete_batch([str(root / "非空" / "子")])
    ck(ok == 0 and "没敢删" in fails[0]["msg"], "里层同样不删", fails)

    print("\n【7】目录联接（junction）：不许顺着链接删到别处去")
    root = fresh("t7")
    target = root / "真的东西"
    mk(target)
    touch(target / "重要.txt", 20)
    link = str(root / "联接")
    # cmd 说的是中文（GBK），按系统内码读，免得解码炸在半路
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, str(target)],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    if r.returncode != 0:
        print(f"  - 跳过（这台机器建不了 junction：{r.stderr.strip()[:40]}）")
    else:
        fs = lf.LocalFiles(root)
        # 联接自己指向的地方有文件 ⇒ 既不许删联接，更不许顺着它删到目标里去
        ok, fails = fs.delete_batch([link])
        ck(ok == 0, "★ 联接没被当成空目录处理", fails)
        ck((Path(target) / "重要.txt").exists(), "★ 联接指向的真数据没被删", None)
        inner = root / "装着联接的目录"
        mk(inner)
        # 再问一次「子树里藏着联接」的情形：整棵不许删
        subprocess.run(["cmd", "/c", "mklink", "/J", str(inner / "link2"),
                        str(target)], capture_output=True, text=True,
                       encoding="gbk", errors="replace")
        ok, fails = fs.delete_batch([str(inner)])
        ck(ok == 0 and any("联接" in f["msg"] for f in fails),
           "★ 子树里有联接时整棵不删，并说清是联接", fails)
        ck((Path(target) / "重要.txt").exists(), "真数据仍然健在", None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
