# -*- coding: utf-8 -*-
r"""
preview_store.py —— 预览文件：把「生成预览」的结果落盘，避免反复重扫网盘
=========================================================================

要解决的问题：改名 / 整理 / 删除的预览都必须先把网盘**递归扫一遍**。沙盒里几万
个文件时一次扫描要好几分钟；而实际用起来常常是「看一眼预览 → 改个参数 → 再看一
眼」，每次都重扫纯属白等。

做法：每次生成预览都存成一份**预览文件**（previews/<时间戳>-<类型>.json），并在
里面记一份「指纹」——影响结果的全部输入（类型 / 目录 / 是否递归 / 模式 / 参数 /
沙盒 / 筛选条件）的 sha1。于是面板上就有了三件事：

  复用    再点「生成预览」时先按指纹找同参数的旧预览，命中就直接拿来用，不碰网盘
  选择    列出所有预览文件，挑一份载入即可执行——不必重新扫描
  重扫    「重新生成」带 refresh=true 过来，跳过复用，老老实实重扫一遍

两条红线：

  1. 参数变一个字指纹就变 —— 绝不会把「按大类」的旧预览当成「按后缀」的结果给你。
  2. **执行过的预览不再复用** —— 执行完网盘已经不是当时的样子了，那份 ops 里可能
     有一半路径已经不存在。宁可让人重扫一次，也不能拿过期清单去动网盘。

存储：<项目目录>/previews/*.json，一份预览一个文件（可以直接翻看、手工删）。最多留
MAX_KEEP 份，超了自动丢最旧的。存盘失败绝不影响「生成预览」本身——只是少个缓存。
"""

import hashlib
import json
import re
import time
from pathlib import Path

DIR_NAME = "previews"
MAX_KEEP = 50                  # 最多保留多少份预览文件（超了丢最旧的）
REUSE_MAX_AGE = 24 * 3600      # 同参数预览超过一天就不再自动复用
MAX_OPS_SAVED = 2000           # 与后端执行上限一致，再多也执行不了，不必占地方
SCAN_LOOKBACK = 40             # 找同参数旧预览时最多翻这么多份（免读一屋子文件）

# 预览文件 id 只允许这些字符：路径分隔符和点都在外面，'..' 这类越界读天然不可能
_ID_OK = re.compile(r"^[0-9A-Za-z_\-]{6,80}$")


def preview_dir(root) -> Path:
    return Path(root) / DIR_NAME


def fingerprint(kind, payload, sandbox=""):
    """把「影响结果的全部输入」压成一个短指纹

    payload 里只放真正会改变结果的字段（目录、递归、模式、参数、筛选条件…），不
    放时间戳之类的易变值——否则每次算出来都不一样，缓存等于没有。sort_keys 保证
    字典的书写顺序不影响结果。
    """
    key = json.dumps({"kind": kind, "sandbox": sandbox, "payload": payload},
                     ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _ts(t0):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))


def _safe_path(root, pid):
    """id → 文件路径。id 是外面传进来的，必须挡住 ../ 这类越界读"""
    if not _ID_OK.match(pid or ""):
        return None
    return preview_dir(root) / f"{pid}.json"


def _files(root):
    """全部预览文件，按时间倒序（新 → 旧）"""
    d = preview_dir(root)
    if not d.is_dir():
        return []
    fs = [f for f in d.glob("*.json") if f.is_file()]
    # 用文件 mtime 而不是文件里的 t0 排序：万一有一条是坏文件，也不会拖垮整个列表。
    # 名字做次级键——同一秒里连着存两份时文件名带 -2 后缀，靠它才能稳定地让新的在前
    try:
        fs.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    except Exception:
        pass
    return fs


def _read(f):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None      # 坏文件直接跳过，不该让列表整个读不出来


def _write(path, rec):
    """原子写：先写 .tmp 再替换，中途断电也不会留下半截 JSON"""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def summary(rec):
    """预览文件的摘要（列表和返回值用，不含 ops——几万条会撑爆响应）"""
    now = time.time()
    ex = rec.get("executed") or {}
    t0 = rec.get("t0") or 0
    return {
        "id": rec.get("id", ""),
        "ts": rec.get("ts", "") or (_ts(t0) if t0 else ""),
        "t0": t0,
        "age": int(max(0, now - t0)) if t0 else 0,
        "kind": rec.get("kind", ""),
        "label": rec.get("label") or rec.get("kind", ""),
        "path": rec.get("path", ""),
        "total": int(rec.get("total") or 0),
        "executed": bool(ex),
        "executed_ts": ex.get("ts", ""),
        "exec_msg": ex.get("msg", ""),
    }


def save_preview(root, kind, ops, info, payload, sandbox="", path="", label=""):
    """落盘一份预览 → 返回摘要（含 id）。

    任何异常都吞掉返回 None：存不下只是少了一个缓存，绝不能让「生成预览」整个失败。
    """
    try:
        d = preview_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        pid, n = f"{base}-{kind}", 1
        while (d / f"{pid}.json").exists():     # 同一秒连着存两份的情况
            n += 1
            pid = f"{base}-{kind}-{n}"
        rec = {
            "id": pid,
            "t0": now,
            "ts": _ts(now),
            "kind": kind,
            "label": label or kind,
            "sandbox": sandbox,
            "path": path,
            "fingerprint": fingerprint(kind, payload, sandbox),
            "payload": payload,
            "ops": list(ops or [])[:MAX_OPS_SAVED],
            "total": int((info or {}).get("total") or len(ops or [])),
            "info": {k: v for k, v in (info or {}).items() if k != "ops"},
            "executed": None,
        }
        _write(d / f"{pid}.json", rec)
        prune(root)
        return summary(rec)
    except Exception:
        return None


def list_previews(root, limit=500):
    out = []
    for f in _files(root)[:limit]:
        rec = _read(f)
        if rec:
            out.append(summary(rec))
    return out


def load_preview(root, pid):
    p = _safe_path(root, pid)
    if not p or not p.is_file():
        return None
    return _read(p)


def find_recent(root, fp, max_age=REUSE_MAX_AGE):
    """找出可复用的同参数预览；没有就返回 None（调用方老老实实重扫）"""
    now = time.time()
    for f in _files(root)[:SCAN_LOOKBACK]:
        rec = _read(f)
        if not rec or rec.get("fingerprint") != fp:
            continue
        # 最新的那份同参数预览已经执行过 → 网盘变了，不能拿它当「当前状态」用
        if rec.get("executed"):
            return None
        if now - (rec.get("t0") or 0) > max_age:
            return None
        return rec
    return None


def mark_executed(root, pid, stat=None, msg=""):
    """执行完盖一个「已执行」戳：列表里看得出来，也不再被复用"""
    p = _safe_path(root, pid)
    if not p or not p.is_file():
        return False
    try:
        rec = _read(p) or {}
        rec["executed"] = {"ts": _ts(time.time()), "stat": stat or {}, "msg": msg or ""}
        _write(p, rec)
        return True
    except Exception:
        return False


def delete_preview(root, pid):
    p = _safe_path(root, pid)
    if not p or not p.is_file():
        return False
    try:
        p.unlink()
        return True
    except Exception:
        return False


def clean_executed(root):
    """删掉所有「已执行过」的预览文件 → 返回删掉的份数"""
    n = 0
    for f in _files(root):
        rec = _read(f)
        if rec and rec.get("executed"):
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
    return n


def prune(root, keep=MAX_KEEP):
    """只留最新的 keep 份，其余删掉 → 返回删掉的份数。

    顺带清掉上次异常中断留下的 .tmp（超过 1 小时还没被替换掉的，肯定是垃圾）。
    """
    n = 0
    for f in _files(root)[keep:]:
        try:
            f.unlink()
            n += 1
        except Exception:
            pass
    try:
        for t in preview_dir(root).glob("*.json.tmp"):
            try:
                if time.time() - t.stat().st_mtime > 3600:
                    t.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return n
