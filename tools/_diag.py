# -*- coding: utf-8 -*-
"""诊断小工具：查某个词在整库里的分布，看它到底像作者还是像书名。

用法： python tools/_diag.py 柳残阳 温瑞安 安妮宝贝 卧龙生 还珠楼主
"""
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import novel_clean as nc

rows, _ = nc.load_rows()
stems = [nc.base_stem(r["文件名"], (r.get("后缀") or "").strip()) for r in rows]
nc.set_author_vocab(stems)

cnt = collections.Counter()
head = collections.Counter()
alone = collections.Counter()
for s in stems:
    segs = [x for x in nc.SEG_SPLIT.split(s) if x and nc.CJK.search(x)]
    for i, x in enumerate(segs):
        cnt[x] += 1
        if i == 0:
            head[x] += 1
    if len(segs) == 1:
        alone[segs[0]] += 1

names = sys.argv[1:] or ["柳残阳", "温瑞安", "安妮宝贝", "卧龙生",
                         "还珠楼主", "寄秋", "卫斯理", "郑渊洁"]
for n in names:
    c = cnt[n]
    print(f"{n}\t总{c}\t首段{head[n]}({head[n]/c:.2f})\t"
          f"单独{alone[n]}({alone[n]/c:.2f})\t"
          f"AUTHOR={n in nc.AUTHOR_VOCAB}\tPREFIX={n in nc.PREFIX_VOCAB}")

print()
print("PREFIX_VOCAB 大小：", len(nc.PREFIX_VOCAB))
print()
print("AUTHOR_VOCAB 大小：", len(nc.AUTHOR_VOCAB))
print()
# 候选「人名表」：出现够多、且几乎从不单独成段（书名会单独出现，作者不会）
import random
CJK_RE = re.compile(r"^[" + nc.CJK_CH + r"]{2,4}$")
cand = {x for x, c in cnt.items()
        if c >= 20 and alone[x] / c <= 0.03 and CJK_RE.match(x)}
print("NAME_VOCAB 候选：", len(cand))
print("样例：", sorted(cand)[:200])
for probe in ["红楼梦", "三国演义", "西游记", "水浒传", "盗墓笔记",
              "卫斯理", "今古传奇", "全集", "小说", "精品", "正文",
              "目录", "简介", "新修", "当代", "收获", "九州"]:
    if probe in cnt:
        c = cnt[probe]
        print(f"  probe {probe}\t总{c}\t单独{alone[probe]}({alone[probe]/c:.3f})"
              f"\t入选={probe in cand}")
