# -*- coding: utf-8 -*-
r"""
local_rename.py —— 上传前把本地文件名清理干净
==============================================

为什么不放进 pan_tools.py：那支管的是网盘上的文件，犯错了还能重来；这支动的是
你磁盘上的**真文件**，风险完全不同。所以单独一支，并且：

  1. 扫描口径与 upload_baidu.collect_files 完全一致（同一套排除规则、同样跳过
     空文件和超大文件）——预览说会改 N 个，真上传时就得是同一批，不能对不上号。
  2. 逐条 os.rename，**绝不覆盖**已存在的文件（Windows 上 os.rename 撞名会直接
     失败，这里再显式挡一道，跳过并记原因）。
  3. 每次改名都追加一批记录到 local_rename_log.jsonl，支持整批回退。

命名规则复用 pan_tools（title / clean / replace / affix / serial / regex 同一套），
免得「先改名再上传」和「上传后在网盘改名」出现两套结果。
"""

import fnmatch
import json
import os
import time
from pathlib import Path

import pan_tools

LOG_NAME = "local_rename_log.jsonl"
DEFAULT_EXCLUDES = ["Thumbs.db", "desktop.ini", "*.tmp", "~$*", "*.partial"]
# 本工具自己产生的记录文件，永远不参与改名
_SELF_FILES = {LOG_NAME, "uploaded_log.txt", "uploaded_log.json", "upload_map.json",
               "token.json", "config.json", "progress.json", "upload.pid"}

# 改名模式的中文名，给提示文案用
MODE_LABELS = {
    "title": "只保留书名",
    "clean": "去广告/括号清理",
    "replace": "查找替换",
    "affix": "加前后缀",
    "serial": "序号重命名",
    "regex": "正则替换",
}


def scan_local(cfg, root=None):
    """按上传的口径扫本地文件，返回 (文件 Path 列表, 跳过统计)

    与 upload_baidu.collect_files 保持一致：排除规则命中、0 字节、以及超过
    max_file_size_mb 的文件都不改名——它们本来就不会被上传，改了也没意义。
    """
    root = Path(root or cfg.get("local_dir") or "").expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"本地目录不存在：{root}")
    patterns = cfg.get("exclude_patterns") or DEFAULT_EXCLUDES
    # 不能先 int() 再乘：配置里写成 0.5（MB）时 int() 会先变成 0，上限成了 0 字节，
    # 结果一个文件都进不来。跟 upload_baidu.collect_files 一样直接乘。
    max_bytes = float(cfg.get("max_file_size_mb", 4096) or 4096) * 1024 * 1024
    recursive = bool(cfg.get("recursive", True))

    out, skipped = [], {"excluded": 0, "empty": 0, "too_big": 0}
    for p in sorted(root.rglob("*") if recursive else root.glob("*")):
        if not p.is_file():
            continue
        if p.name in _SELF_FILES:
            continue
        rel = p.relative_to(root)
        if any(fnmatch.fnmatch(p.name, pat) or fnmatch.fnmatch(str(rel), pat)
               for pat in patterns):
            skipped["excluded"] += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0:
            skipped["empty"] += 1
            continue
        if size > max_bytes:
            skipped["too_big"] += 1
            continue
        out.append(p)
    return out, skipped


def plan_local(files, mode="title", params=None, index_by_path=None):
    """算出改名计划，返回 (rows, unchanged)

    row = {"dir", "path", "name", "newname", "auto"}
    auto=True 表示这条是因为撞名才被加上 (2)(3) 的，前端要如实标出来。
    """
    params = params or {}
    rows, unchanged = [], 0
    for i, p in enumerate(files):
        idx = index_by_path(p) if index_by_path else i
        new = pan_tools.rename_one(p.name, mode, params, idx)
        if new and new != p.name:
            rows.append({"dir": str(p.parent), "path": str(p), "name": p.name,
                         "newname": new})
        else:
            unchanged += 1
    auto = _dedupe_against_disk(rows)
    return rows, unchanged


def _dedupe_against_disk(rows):
    """撞名时自动加 (2)(3)…，并在 row 上打 auto 标记

    占用表取自磁盘实况（os.scandir），但**排除掉本批即将改名的那些名字**——
    它们马上就让位了。不这么算的话，一条链式改名（a→b、b→c）会被误判成撞车。
    与 pan_tools.dedupe_newnames 是同一套编号规则，两边行为一致。
    """
    if not rows:
        return 0
    renaming = {(r["dir"], r["name"].lower()) for r in rows}
    occupied = {}
    for d in {r["dir"] for r in rows}:
        names = set()
        try:
            for e in os.scandir(d):
                if e.is_file():
                    names.add(e.name.lower())
        except OSError:
            pass
        occupied[d] = {n for n in names if (d, n) not in renaming}
    before = [r["newname"] for r in rows]
    n = pan_tools.dedupe_newnames(rows, occupied)
    for r, b in zip(rows, before):       # 标出哪些是被自动编号的，前端要如实显示
        r["auto"] = r["newname"] != b
    return n


def apply_local(rows, log_path):
    """真的改名。返回 (done, fails)

    done: [{"dir","old","new"}]  —— 已写入改名记录，可整批回退
    fails: [{"path","msg"}]
    """
    done, fails = [], []
    for r in rows:
        src = Path(r["path"])
        dst = Path(r["dir"]) / r["newname"]
        # 只改大小写的重命名（A.txt → a.txt）在 Windows 上 dst 也算「已存在」，
        # 那是同一个文件，不是撞名，必须放行。
        if dst.exists() and str(dst).lower() != str(src).lower():
            fails.append({"path": str(src), "msg": f"目标已存在，跳过：{r['newname']}"})
            continue
        try:
            os.rename(str(src), str(dst))
        except OSError as e:
            fails.append({"path": str(src), "msg": e.strerror or str(e)})
            continue
        done.append({"dir": r["dir"], "old": r["name"], "new": r["newname"]})

    if done:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "items": done,
               "undone": False}
        lp = Path(log_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return done, fails


def read_log(log_path):
    """读改名记录，返回 [{ts, items, undone}, ...]（坏行直接跳过，不炸）"""
    lp = Path(log_path)
    if not lp.exists():
        return []
    out = []
    for line in lp.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("items"), list):
            out.append(rec)
    return out


def last_undoable(log_path):
    """最近一批还没回退过的改名记录，没有则 None"""
    for rec in reversed(read_log(log_path)):
        if not rec.get("undone"):
            return rec
    return None


def undo_last(log_path):
    """回退最近一批改名。返回 (done, fails, rec)"""
    recs = read_log(log_path)
    idx = None
    for i in range(len(recs) - 1, -1, -1):      # 从后往前找第一批没回退过的
        if not recs[i].get("undone"):
            idx = i
            break
    if idx is None:
        return [], [], None
    rec = recs[idx]

    done, fails = [], []
    for it in rec["items"]:
        src = Path(it["dir"]) / it["new"]
        dst = Path(it["dir"]) / it["old"]
        if not src.exists():
            fails.append({"path": str(src), "msg": "文件已不在（可能被移动或删除）"})
            continue
        if dst.exists() and str(dst).lower() != str(src).lower():
            fails.append({"path": str(src), "msg": f"原名已被占用，跳过：{it['old']}"})
            continue
        try:
            os.rename(str(src), str(dst))
        except OSError as e:
            fails.append({"path": str(src), "msg": e.strerror or str(e)})
            continue
        done.append({"dir": it["dir"], "old": it["new"], "new": it["old"]})

    # 就地标记已回退：允许「改错了 → 回退 → 再改一次」的来回操作，
    # 但不能把同一批回退两遍（第二遍必然报一堆「文件已不在」）。
    rec["undone"] = True
    rec["undo_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    recs[idx] = rec
    Path(log_path).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")
    return done, fails, rec
