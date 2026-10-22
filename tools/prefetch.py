# -*- coding: utf-8 -*-
"""给一批待查书名做「预判」，把明显不该查的挡在联网之外。

为什么需要：待查清单里有很大一部分**根本不是书**——
`醉逍遥书屋`（站点水印）、`VIP会员必读说明`（站内公告）、`财经`
（杂志栏目）、`驱动程序更新快报`（硬件站栏目）。这些名字拿去搜，
搜到的也是无关网页，白烧几千次查询。

另外还有一大类是「作者已经写在名字里了」：
`宫崎市定`（就是作者本人）、`三毛`（作者名，和书名同形）。
查它等于让搜索引擎把作者名再当书名返回一遍，会把作者记成书名。

用法：
    python tools/prefetch.py            # 预判全部待查，写进 authors_pending
    python tools/prefetch.py stat       # 只看分布
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import novel_clean as nc                        # noqa: E402

OUT = nc.OUT
AUTHORS_JSON = os.path.join(OUT, "authors.json")
PENDING_JSON = os.path.join(OUT, "authors_pending.json")
SKIP_JSON = os.path.join(OUT, "authors_skipped.json")

# 站点水印 / 站内公告 / 栏目名 —— 这些不是书，搜了也没作者
NOT_A_BOOK = {
    "醉逍遥书屋", "原创天地", "VIP会员必读说明", "专家坐堂", "追新一族",
    "城市硬件DIY情报站", "软件一点通", "驱动程序更新快报", "硬件瞭望台",
    "玖头在线", "财经", "哈佛商业评论", "中国全史", "长篇小说的选刊",
    "第一", "的三生石",
}
NOT_BOOK_PAT = re.compile(
    r"(书屋|书城|书库|论坛|社区|网站|网盘|下载站|小说网|文学网|"
    r"必读说明|会员|公告|通知|投稿|征稿|更新快报|驱动程序|硬件|"
    r"情报站|瞭望台|在线|编辑部|杂志社)$")


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
    os.replace(tmp, path)


def decide(title):
    """返回 (判定, 理由)。判定 ∈ {'skip', 'author-self', None}"""
    t = title.strip()
    if not t:
        return "skip", "空名"
    if t in NOT_A_BOOK or NOT_BOOK_PAT.search(t):
        return "skip", "站点/栏目名"
    # 只剩一个字的（`脊`、`酒`）是切碎的残渣
    if len(nc.CJK.findall(t)) <= 1:
        return "skip", "单字残渣"
    return None, ""


def cmd_run():
    lib = _load(AUTHORS_JSON, {})
    pend = _load(PENDING_JSON, [])
    skipped = _load(SKIP_JSON, {})
    cnt = {"skip": 0, "author-self": 0, "left": 0}
    for p in pend:
        if p.get("state", "todo") != "todo":
            continue
        t = p["title"]
        if t in lib:
            p["state"] = "done"
            continue
        verdict, why = decide(t)
        if verdict == "skip":
            p["state"] = "checked-none"
            skipped[t] = why
            cnt["skip"] += 1
        else:
            cnt["left"] += 1
    _save(lib, AUTHORS_JSON)
    _save(pend, PENDING_JSON)
    _save(skipped, SKIP_JSON)
    todo = sum(1 for p in pend if p.get("state") == "todo")
    print(f"预判：挡掉 {cnt['skip']} 条，剩 {todo} 条真待查")


def cmd_stat():
    pend = _load(PENDING_JSON, [])
    st = {}
    for p in pend:
        st[p.get("state", "todo")] = st.get(p.get("state", "todo"), 0) + 1
    print("待查清单状态：", st)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stat":
        cmd_stat()
    else:
        cmd_run()
