# -*- coding: utf-8 -*-
r"""第五轮最后一刀：色情站广告 + 纯卷号碎片

两个坑都踩过了，记在这里：
  1. 别在 shell 的 `python -c "..."` 里写 `\$\$` —— bash 双引号会把 `\$`
     还原成 `$`，正则里就成了两个零宽行尾锚点，**匹配每一行**。
  2. 「小电影」「一本道」不能单独当广告判据：《大熊猫看小电影》是沈宏非的
     随笔集，《一本道德闯异界》是本正经网文。必须跟下载/在线这些词同现才算。
"""
import csv, re, os, sys, shutil, collections

CSV = r"C:\Users\UserData\Desktop\文件清单_apps_baidu_uploader_归档_完整.csv"
DOLLAR = "$$"          # 用变量拼，绕开转义坑

# 见 docstring 坑 2：分强/弱两档，弱档要凑齐上下文才算广告
STRONG = re.compile(r"(在线看|在線看|色情电影|成人电影|色站|AV女优|快播|"
                    r"种子下载|種子下載|一本道(?!德)|"
                    + re.escape(DOLLAR) + r")")
WEAK = re.compile(r"(小电影|小電影)")
WEAK_CTX = re.compile(r"(下载|下載|在线|在線|免费|免費|观看|觀看|全集|精品)")

FRAG = re.compile(r"^卷\s*[0-9零一二三四五六七八九十百千万两]{1,6}$")


def main():
    write = "--write" in sys.argv
    rows = list(csv.reader(open(CSV, encoding="utf-8-sig", newline="")))
    hdr = rows[0]
    i_op, i_new, i_old = hdr.index("操作"), hdr.index("新文件名"), hdr.index("文件名")

    def eff(r):
        return (r[i_new] or r[i_old]).strip()

    stat = collections.Counter()
    gone = []
    for r in rows[1:]:
        if r[i_op].strip() == "删除":
            continue
        name = eff(r)
        stem = os.path.splitext(name)[0].strip()
        if STRONG.search(name) or (WEAK.search(name) and WEAK_CTX.search(name)):
            r[i_op], r[i_new] = "删除", ""
            stat["删·色情站广告"] += 1
            gone.append(("广告", name))
        elif FRAG.match(stem):
            r[i_op], r[i_new] = "删除", ""
            stat["删·纯卷号碎片"] += 1
            gone.append(("碎片", name))

    keep = sum(1 for r in rows[1:] if r[i_op].strip() != "删除")
    print("保留:", keep, " 删除:", len(rows) - 1 - keep)
    for k, v in stat.most_common():
        print(f"  {k}\t{v}")
    print("\n=== 删掉的行 ===")
    for tag, n in gone[:80]:
        print(f"  [{tag}] {n}")

    if not write:
        print("\n[试跑] 加 --write 才写盘")
        return
    if not os.path.exists(CSV + ".bak7"):
        shutil.copy2(CSV, CSV + ".bak7")
        print("\n已备份:", CSV + ".bak7")
    with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows[1:])
    print("已写回:", CSV)


if __name__ == "__main__":
    main()
