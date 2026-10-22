# -*- coding: utf-8 -*-
"""找「作者被当成书名」的记录。

样例：`名著《洛丽塔》`、`玄幻《征神领域》`、`名著《飘》`。
书名号前面那个词不是作者，是**分类标签/丛书名/站内前缀**。
一旦它被写进 `作者《书名》` 的位置，看起来就像作者，实际是脏数据。

用法：
    python tools/author_check.py list 200   # 列出最可疑的 200 个「假作者」
    python tools/author_check.py stat       # 总量统计
"""
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import novel_clean as nc                        # noqa: E402

# 「作者」位置上一定不是人名的词
FAKE_AUTHOR = {
    "名著", "经典", "世界名著", "中外名著", "外国名著", "中国名著", "名著导读",
    "玄幻", "武侠", "言情", "都市", "穿越", "科幻", "恐怖", "灵异", "历史",
    "军事", "网游", "仙侠", "修真", "青春", "校园", "古言", "现言", "耽美",
    "同人", "原创", "精品", "推荐", "完本", "全集", "合集", "全本", "连载",
    "小说", "长篇小说", "短篇小说", "中篇小说", "微小说", "网络小说",
    "外国文学", "中国文学", "世界文学", "古典文学", "现代文学", "当代文学",
    "文学", "散文", "诗歌", "随笔", "杂文", "剧本", "影视", "评书",
    "中文版", "全译本", "改写版", "缩写版", "青少版", "彩图版", "插图本",
    "作者", "编著", "主编", "编译", "佚名", "未知", "不详", "无名氏",
}
# 明显是「丛书名/系列名」后缀
FAKE_SUFFIX = ("系列", "丛书", "文集", "选集", "全集", "合集", "书系",
               "名著", "经典", "文库", "典藏", "书库", "专题")


def suspect(author):
    """这个「作者」像不像假的"""
    a = author.strip()
    if not a:
        return "空"
    if a in FAKE_AUTHOR:
        return "分类/版本词"
    if a.endswith(FAKE_SUFFIX):
        return "丛书/系列名"
    if len(nc.CJK.findall(a)) == 0:
        return "非中文"
    if len(a) >= 8:
        return "过长"
    return ""


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    path = os.path.join(nc.OUT, "authors.json")
    nc.load_authors(path)
    bad = {k: v for k, v in nc.AUTHOR_OF.items() if suspect(v)}
    cnt = collections.Counter(suspect(v) for v in bad.values())
    print(f"作者库 {len(nc.AUTHOR_OF)} 本，其中疑似假作者 {len(bad)} 本")
    for k, v in cnt.most_common():
        print(f"  {k:<12} {v}")
    if cmd == "list":
        top = collections.Counter(suspect(v) for v in bad.values()).most_common()
        groups = collections.defaultdict(list)
        for k, v in bad.items():
            groups[suspect(v)].append((k, v))
        print("\n=== 样本 ===")
        for why, _ in top:
            for k, v in sorted(groups[why])[:n // max(1, len(top))]:
                print(f"  [{why}] {v}《{k}》")


if __name__ == "__main__":
    main()
