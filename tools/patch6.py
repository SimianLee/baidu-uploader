# -*- coding: utf-8 -*-
"""第五轮收尾：处理「原名已合格、无需改名」的那批行

apply 阶段对「洗完跟原名一模一样」的行写的是 `操作=""、新文件名=""`，
于是它们带着**原名**留在库里，却没参加过任何一轮删除/去重检查 ——
`｛华文网收集整理｝.PDF` 这种垃圾就是这么漏下来的，
_audit 里那 5 组重名也是它们跟改名后的行撞了名。

这一遍把「有效名 = 新文件名 or 原文件名」统一起来，补齐检查。
"""
import csv, re, os, sys, shutil, collections

CSV = r"C:\Users\UserData\Desktop\文件清单_apps_baidu_uploader_归档_完整.csv"

AD = re.compile(r"(www\.|\.com|\.net|\.org|华文网|收集整理|网收集|"
                r"快眼看书|啃书网|电子书下载|txt下载|免费下载|"
                r"欢迎访问|点击进入|版权归|免责声明|｛[^｝]*整理｝)")

# 单独成篇的零碎：`上篇.doc`、`目录.txt` 不是书
FRAG = re.compile(r"^(上篇|下篇|中篇|上集|下集|前言|序言|序|目录|目次|附录|"
                  r"后记|後記|内容简介|內容簡介|内容提要|楔子|引子|尾声|尾聲|"
                  r"第一章|第一章\s*\(.*\)|封面|封底|版权页|版權頁)$")


def main():
    write = "--write" in sys.argv
    rows = list(csv.reader(open(CSV, encoding="utf-8-sig", newline="")))
    hdr = rows[0]
    i_op, i_new, i_old = hdr.index("操作"), hdr.index("新文件名"), hdr.index("文件名")
    i_ext, i_size = hdr.index("后缀"), hdr.index("大小(字节)")

    def eff(r):
        """有效名：改过名的用新名，没改的用原名"""
        return (r[i_new] or r[i_old]).strip()

    stat = collections.Counter()
    gone = []

    for r in rows[1:]:
        if r[i_op].strip() == "删除":
            continue
        name = eff(r)
        stem = os.path.splitext(name)[0]
        if AD.search(name):
            r[i_op], r[i_new] = "删除", ""
            stat["删·广告垃圾"] += 1
            gone.append(("广告", name))
        elif FRAG.match(stem.strip()):
            r[i_op], r[i_new] = "删除", ""
            stat["删·零碎篇章"] += 1
            gone.append(("零碎", name))

    # 去重：按有效名（含扩展名）分组，每组只留最大的一份
    by = collections.defaultdict(list)
    for r in rows[1:]:
        if r[i_op].strip() != "删除":
            by[eff(r)].append(r)
    dups = []
    for k, g in by.items():
        if len(g) < 2:
            continue
        g.sort(key=lambda r: -(int(r[i_size] or 0)))
        dups.append((k, len(g), [(x[i_old], x[i_new], x[i_size]) for x in g]))
        for extra in g[1:]:
            extra[i_op], extra[i_new] = "删除", ""
            stat["删·去重"] += 1

    keep = sum(1 for r in rows[1:] if r[i_op].strip() != "删除")
    print("保留:", keep, " 删除:", len(rows) - 1 - keep)
    print()
    for k, v in stat.most_common():
        print(f"  {k}\t{v}")

    print("\n=== 删掉的垃圾/零碎 ===")
    for tag, n in gone[:60]:
        print(f"  [{tag}] {n}")

    print("\n=== 解掉的重名组 ===")
    for k, n, g in dups:
        print(f"  {k}  x{n}")
        for o, nn, sz in g:
            print(f"      原名={o}  新名={nn or '(同名未改)'}  大小={sz}")

    if not write:
        print("\n[试跑] 加 --write 才写盘")
        return
    if not os.path.exists(CSV + ".bak6"):
        shutil.copy2(CSV, CSV + ".bak6")
        print("\n已备份:", CSV + ".bak6")
    with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows[1:])
    print("已写回:", CSV)


if __name__ == "__main__":
    main()
