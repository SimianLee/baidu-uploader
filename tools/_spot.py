# -*- coding: utf-8 -*-
"""定点回归：拿一批「已知该改成什么样」的文件名，看 pick_title / classify 的输出。

用法： python tools/_spot.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import novel_clean as nc

rows, _ = nc.load_rows()
stems = [nc.base_stem(r["文件名"], (r.get("后缀") or "").strip()) for r in rows]
nc.set_author_vocab(stems)
print("NAME_VOCAB:", len(nc.NAME_VOCAB),
      "AUTHOR_VOCAB:", len(nc.AUTHOR_VOCAB),
      "PREFIX_VOCAB:", len(nc.PREFIX_VOCAB))

CASES = [
    # (文件名, 期望改名)      —— 期望为 None 表示「应该被判死删掉」
    ("柳残阳 沥血伏龙 (2).txt", "沥血伏龙2.txt"),
    ("温瑞安 - 天威.epub", "天威.epub"),
    ("卧龙生 飘花令.txt", "飘花令.txt"),
    ("还珠楼主-蜀山剑侠传.epub", "蜀山剑侠传.epub"),
    ("莲花-安妮宝贝.epub", "莲花.epub"),
    ("春宴-安妮宝贝.epub", "春宴.epub"),
    ("寄秋-茶意绵绵.txt", "茶意绵绵.txt"),
    ("卫斯理--蓝血人.mobi", "蓝血人.mobi"),
    ("卫斯理--地底奇人.mobi", "地底奇人.mobi"),
    ("枭霸-柳残阳_TXT小说天堂.txt", "枭霸.txt"),
    ("永生_梦入神机_TXT小说天堂.txt", "永生.txt"),
    ("郑渊洁-智齿.epub", "智齿.epub"),
    ("全唐文-清-董诰.txt", "全唐文.txt"),
    ("水浒传-明-施耐庵.txt", "水浒传.txt"),
    ("郝恬谧的生活.txt", "郝恬谧的生活.txt"),
    ("萧十一郎（上）.pdf", "萧十一郎-上部.pdf"),
    ("边荒传说（下）.6寸版.pdf", "边荒传说-下部.pdf"),
    # 「1-4部」是合订本，不是第 1 部，所以不该加卷号
    ("后宫甄嬛传（1-4部）-流潋紫.epub", "后宫甄嬛传.epub"),
    ("汤姆克朗西--猎杀红色十月.mobi", "猎杀红色十月.mobi"),
    ("雪中悍刀行-烽火戏诸侯.txt", "雪中悍刀行.txt"),
    ("大唐双龙传 - 黄易.mobi", "大唐双龙传.mobi"),
    ("诛仙-萧鼎.txt", "诛仙.txt"),
    ("余华-鲜血梅花 (3).txt", "鲜血梅花3.txt"),
    ("悲惨世界-(法)雨果(Hugo.V.),李玉民.epub", "悲惨世界.epub"),
    # 联网核实过是真小说：《断虹金钩》武陵樵子的武侠；
    # 《刘公案》清代公案小说（与包公案/施公案/狄公案并称四大公案小说）
    ("断虹金钩.txt", "断虹金钩.txt"),
    ("刘公案21.txt", "刘公案21.txt"),
    ("第一章 迎来送往.txt", "迎来送往.txt"),
    ("迟来的白马要不要.txt", "迟来的白马要不要.txt"),
    ("14飞狐的故事－孔庆东.txt", "飞狐的故事.txt"),
    ("爱，那么痛......之微寒.txt", "爱，那么痛之微寒.txt"),
    ("第六章第一百零一节笔仙之祸1.txt", "笔仙之祸1.txt"),
    # 下面这两个靠 main() 里「改名后只剩一个汉字」的规则清掉，classify 管不到
    #   ("脊.txt", None), ("第251.txt", None),
    # 判死
    ("目  录 (24).txt", None),
    ("序15.txt", None),
    ("百度知道1967.txt", None),
    ("选 股 绝 招与股市宝典.TXT", None),
    ("正统道藏正一部-道法会元-明-佚名.txt", None),
    ("中国撰述诸宗著述部天台宗-四教仪注汇补辅宏记-清-性权.txt", None),
    ("诺贝尔文学奖作品典藏书系全集-S.拉格洛芙.epub", None),
    ("★本文件夹所有TXT文件列表★_by完结小说网 (27).txt", None),
    ("刘兰芳-评书-杨家将.txt", None),
    ("B 小说类 恐怖 (10).txt", None),
    ("[德国]施笃姆作品集.chm", None),
]

bad = 0
for name, want in CASES:
    ext = os.path.splitext(name)[1].lstrip(".") or "txt"
    bad_del, why = nc.classify(name, ext)
    got = None
    if not bad_del:
        got = nc.clean_name(name, ext, allow_bare=False)[0]
    ok = (got == want)
    if not ok:
        bad += 1
    flag = "OK " if ok else "XX "
    print(f"{flag}{name:<46} 期望={want}  实际={got or '删除('+why+')'}")

print()
print("不通过：", bad, "/", len(CASES))
