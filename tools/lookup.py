# -*- coding: utf-8 -*-
"""联网补作者：一个给「我」（Agent）用的记账工具。

流程是人工/AI 混合的：
  1. `python tools/lookup.py next 40`   —— 吐出接下来的 40 个待查书名；
  2. 我拿这些书名去联网搜索，得到作者；
  3. `python tools/lookup.py set 书名=作者 书名=作者 ...`
                                       —— 把结果写回作者库；
  4. 重复，直到查完或决定停。

为什么不做成「脚本自己爬」：网络搜索是会话内的工具，不是脚本能调的 API；
直接在脚本里爬网页几分钟就会被封，几十万次请求也不现实。
这个工具把「查」和「记」拆开，查找由会话完成，记账是本地即时落盘的，
任何时候中断都不丢进度。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import novel_clean as nc                        # noqa: E402

OUT = nc.OUT
AUTHORS_JSON = os.path.join(OUT, "authors.json")
PENDING_JSON = os.path.join(OUT, "authors_pending.json")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)          # 原子替换，中断也不会写坏


def cmd_next(n=30):
    pend = _load(PENDING_JSON, [])
    lib = _load(AUTHORS_JSON, {})
    todo = [p for p in pend if p["state"] == "todo" and p["title"] not in lib]
    todo.sort(key=lambda p: -p["files"])
    print(f"待查 {len(todo)} 本，下面是最急的 {min(n, len(todo))} 本：")
    for p in todo[:n]:
        print(f"  {p['files']:>4}  {p['title']}")


def cmd_set(pairs):
    lib = _load(AUTHORS_JSON, {})
    pend = _load(PENDING_JSON, [])
    by_title = {p["title"]: p for p in pend}
    ok = 0
    for item in pairs:
        if "=" not in item:
            continue
        title, author = item.split("=", 1)
        title, author = title.strip(), author.strip()
        if not title:
            continue
        if author in ("", "-", "无", "未知"):
            # 查不到 / 确认没有作者：记成已查，避免下次重复查
            if title in by_title:
                by_title[title]["state"] = "checked-none"
            continue
        lib[title] = {"author": author, "source": "web", "evidence": "联网",
                      "votes": 1}
        if title in by_title:
            by_title[title]["state"] = "done"
        ok += 1
    _save(lib, AUTHORS_JSON)
    _save(pend, PENDING_JSON)
    print(f"写入 {ok} 条，作者库现有 {len(lib)} 本")


def cmd_stat():
    lib = _load(AUTHORS_JSON, {})
    pend = _load(PENDING_JSON, [])
    st = {"todo": 0, "done": 0, "checked-none": 0}
    for p in pend:
        st[p.get("state", "todo")] = st.get(p.get("state", "todo"), 0) + 1
    src = {}
    for v in lib.values():
        src[v.get("source", "?")] = src.get(v.get("source", "?"), 0) + 1
    print(f"作者库 {len(lib)} 本　按来源 {src}")
    print(f"待查清单 {len(pend)} 本　{st}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] == "stat":
        cmd_stat()
    elif sys.argv[1] == "next":
        cmd_next(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    elif sys.argv[1] == "set":
        cmd_set(sys.argv[2:])
    else:
        print(__doc__)
