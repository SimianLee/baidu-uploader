# -*- coding: utf-8 -*-
"""从文件名里把「本来就写着的作者名」挖出来，建成本地作者库。

思路：
  1. 用 novel_clean 的清洗流程拿到每个文件的**最终书名**（这个已经调好了）；
  2. 再从**原始文件名**里找作者 —— 作者只会出现在几种固定位置：
       · `作者：某某` / `某某著` 这类显式标记（最可靠）
       · `郑渊洁-智齿` / `寄秋《茶意绵绵》` 这类分隔符两侧
       · `柳残阳 沥血伏龙` 这类空格分隔
  3. 同一条记录里如果同时出现了「最终书名」和「作者候选」，就建立
     「书名 → 作者」的映射；同一本书的多个副本会反复投票，票多的算数。

产出的 authors.json 是**后续联网补齐的基础**：
  · 本地能确定的，联网时跳过；
  · 联网查到的，写进同一个文件（加 source 字段区分）。

用法：
    python tools/authors.py build      # 扫全库，生成本地作者库
    python tools/authors.py export     # 导出待联网查询的书名清单
    python tools/authors.py stat       # 看看现在覆盖了多少
"""
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import novel_clean as nc                     # noqa: E402

OUT = nc.OUT
AUTHORS_JSON = os.path.join(OUT, "authors.json")
PENDING_JSON = os.path.join(OUT, "authors_pending.json")

# ------------------------------------------------------------ 作者标记
# 显式标记：作者：某某 / 著者:某某 / 某某 著 / 某某 编著
RE_MARK_AUTHOR = re.compile(
    r"(?:作者|著者|譯者|译者|原著)\s*[:：]\s*([^\s\-_()（）\[\]【】《》,，、]{2,12})")
RE_SUFFIX_ZHU = re.compile(
    r"[（(【\[]?\s*([\u4e00-\u9fff]{2,4})\s*[著編编譯译]\s*$")

# 括注里的作者：`《茶意绵绵》（寄秋）`、`书名(作者：寄秋)`
RE_BRACKET_AUTHOR = re.compile(
    r"[（(【\[]\s*(?:作者\s*[:：]\s*)?([\u4e00-\u9fff]{2,4})\s*[)）】\]]")

# 分隔符两侧：`郑渊洁-智齿`、`寄秋《茶意绵绵》`、`柳残阳 沥血伏龙`
SEP = r"[-\-—–―－_·]"


def _title_of(name, ext):
    """拿最终书名（不含扩展名）。被判死或取不出名的返回空串。"""
    bad, _why = nc.classify(name, ext)
    if bad:
        return ""
    new, _suffix, _changed, _ord = nc.clean_name(name, ext)
    if not new:
        return ""
    # clean_name 现在输出 `作者《书名》.ext` 或 `《书名》.ext`，
    # 这里要的是纯书名：剥掉书名号、也剥掉可能带上的作者前缀。
    stem = os.path.splitext(new)[0]
    m = re.fullmatch(r"[^《》]{0,12}《(.+)》", stem)
    return m.group(1) if m else stem


def _ok_author(cand):
    """候选是不是像个真作者"""
    if not cand or len(cand) < 2 or len(cand) > 5:
        return False
    if cand in NOT_AUTHOR:
        return False
    # `智齿(二十)` 里的「二十」是章节序号，不是作者。
    # 只由数词组成的 2~4 字候选一律拒掉。
    if re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", cand):
        return False
    # 出版社/品牌：`果麦经典`、`读客`、`台版` 这类不是作者
    if re.search(r"(经典|文库|出版|文化|传媒|书系|版本|台版|港版|新版|旧版|"
                 r"简写|缩写|改写|节译|白话|全译|选译|注释|评点|插图|彩图|"
                 r"珍藏|典藏|修订|增补|全本|节本)", cand):
        return False
    if NON_PERSON_PAT.search(cand):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff·.]+", cand):
        return False
    return True


def _author_candidates(raw, title):
    """在一行原始文件名里找作者候选，返回 [(候选, 依据)]，按可信度排序"""
    cands = []
    stem = nc.base_stem(raw, os.path.splitext(raw)[1].lstrip("."))
    stem = nc.to_simplified(stem)

    # 1) 显式标记最可信
    for m in RE_MARK_AUTHOR.finditer(stem):
        cands.append((m.group(1), "标记"))
    for m in RE_SUFFIX_ZHU.finditer(stem):
        cands.append((m.group(1), "著尾"))

    # 2) 书名确定之后，看它前面/后面剩下了什么
    if not title:
        return cands
    idx = stem.find(title)
    if idx < 0:
        # 书名可能是繁体/变体，试一次不去重名的比对
        return cands
    left = stem[:idx].strip(" " + SEP + "《》（）()[]【】")
    right = stem[idx + len(title):].strip(" " + SEP + "《》（）()[]【】")

    # `寄秋《茶意绵绵》` / `郑渊洁-智齿`：书名在右，左段是作者
    if left:
        left_core = re.split(r"[\s\-—–―－_·,，、]+", left)[-1]
        if (re.fullmatch(r"[\u4e00-\u9fff]{2,4}", left_core)
                and not re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", left_core)):
            cands.append((left_core, "前置"))
    # `茶意绵绵-寄秋`：书名在左，右段是作者
    if right:
        right_core = re.split(r"[\s\-—–―－_·,，、]+", right)[0]
        # 带前导零的是副本序号 `(007)`，纯数词的是章节号 `(二十)` —— 都不是作者
        if (re.fullmatch(r"[\u4e00-\u9fff]{2,4}", right_core)
                and not re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", right_core)):
            cands.append((right_core, "后置"))

    for m in RE_BRACKET_AUTHOR.finditer(stem):
        cands.append((m.group(1), "括注"))
    return cands


# 这些词长得像人名（2~4 字汉字），其实是版本说明/分类/下载站水印，
# 一旦被当成作者，整本书的名字就毁了 —— 实测「全集」一下子就占了上万条。
NOT_AUTHOR = {
    # 版本说明
    "全集", "合集", "文选", "文集", "出书版", "网络版", "精校版", "文字版",
    "扫描版", "完整版", "全本", "完结", "连载", "连载中", "已完结", "全本完结",
    "免费版", "正式版", "修订版", "增订版", "插图版", "典藏版", "珍藏版",
    "简体版", "繁体版", "校对版", "手打版", "完结版", "新修版", "重制版",
    # 分类标签
    "都市言情", "言情小说", "武侠小说", "玄幻小说", "网游小说", "穿越小说",
    "青春校园", "现代言情", "古典言情", "外国文学", "中国文学", "当代文学",
    "侦探推理", "科幻世界", "恐怖惊悚", "历史军事", "仙侠修真", "灵异神怪",
    "短篇", "中篇", "长篇", "极短篇", "外传", "番外", "前言", "序言", "后记",
    # 站点水印
    "小说网", "书城", "书库", "书屋", "书屋网", "电子书", "下载站", "分享",
    "整理", "收集", "制作", "录入", "排版", "校对", "提供", "转载",
    # 其它常见误判
    "释义", "导读", "评析", "赏析", "注释", "注解", "今译", "白话", "节选",
    "上册", "下册", "上中下", "第一册", "第二册", "第三册", "套装", "全册",
    # 又一个版本词 + 题材词
    "旧版", "新版", "初版", "原版", "正版", "盗版", "试读", "样章", "存稿",
    "历史", "军事", "历史军事", "都市", "言情", "武侠", "仙侠", "修真",
    "洋人", "外国人", "佚名", "无名氏", "不题撰人", "撰人", "口述", "整理者",
    "美强", "美强生子", "生子", "甜文", "虐文", "清水", "肉文", "NP", "HE",
    "同人", "原作", "原著", "改编", "剧本", "电视剧", "电影", "广播剧",
    # 书内结构的词
    "楔子", "尾声", "结局", "续集", "前传", "附录", "索引", "目录", "正文",
    "卷一", "卷二", "卷三", "第一回", "第二回", "上卷", "下卷", "中卷",
}

# 明显的「非人名」形状：机构式
NON_PERSON_PAT = re.compile(
    r"(公司|大学|学院|出版社|书店|书城|中心|协会|集团|网站|论坛|社区|工作室)")


def build():
    rows, _ = nc.load_rows()
    nc.set_author_vocab([nc.base_stem(r["文件名"], (r.get("后缀") or "").strip())
                         for r in rows])

    # 书名 → Counter(作者候选 → 票数)
    votes = collections.defaultdict(collections.Counter)
    why_of = collections.defaultdict(dict)
    done = 0
    for r in rows:
        name = r["文件名"]
        ext = (r.get("后缀") or "").strip()
        title = _title_of(name, ext)
        if not title:
            continue
        cands = _author_candidates(name, title)
        for cand, why in cands:
            if cand == title or cand in nc.NAME_STOP:
                continue
            if not _ok_author(cand) or nc._looks_cat(cand):
                continue
            # 候选是书名的一部分（`三国之天下我做主` 去掉「三国」剩的）也不是作者
            if len(cand) >= 2 and cand in title:
                continue
            # 分册序号：`敌意之三`、`XX之二`
            if re.search(r"之[一二三四五六七八九十\d]+$", cand):
                continue
            votes[title][cand] += 1
            why_of[title].setdefault(cand, why)
        done += 1
        if done % 50000 == 0:
            print("  扫过", done, "条", flush=True)

    lib = {}
    for title, c in votes.items():
        cand, n = c.most_common(1)[0]
        # 只投一票的容易是误判（比如 `红楼梦 脂砚斋评` 里的「脂砚斋」），
        # 但只要那个候选在整库作者表里、或者票数 ≥2，就收下。
        if n >= 2 or cand in nc.AUTHOR_VOCAB or cand in nc.NAME_VOCAB:
            lib[title] = {"author": cand, "source": "local",
                          "evidence": why_of[title][cand], "votes": n}
        elif n == 1:
            lib[title] = {"author": cand, "source": "local-weak",
                          "evidence": why_of[title][cand], "votes": 1}
    with open(AUTHORS_JSON, "w", encoding="utf-8") as f:
        json.dump(lib, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f"本地作者库：{len(lib)} 本书，写入 {AUTHORS_JSON}")
    strong = sum(1 for v in lib.values() if v["source"] == "local")
    print(f"  其中强证据 {strong} 本，弱证据（仅 1 票）{len(lib) - strong} 本")


def _all_titles():
    """全库所有最终书名 → 出现次数"""
    rows, _ = nc.load_rows()
    nc.set_author_vocab([nc.base_stem(r["文件名"], (r.get("后缀") or "").strip())
                         for r in rows])
    cnt = collections.Counter()
    for r in rows:
        t = _title_of(r["文件名"], (r.get("后缀") or "").strip())
        if t:
            cnt[t] += 1
    return cnt


def export():
    cnt = _all_titles()
    lib = {}
    if os.path.exists(AUTHORS_JSON):
        with open(AUTHORS_JSON, encoding="utf-8") as f:
            lib = json.load(f)
    # 只导出「值得联网查」的：排除正文/讲义/作品相关这类结构名，
    # 以及只剩一两个字的碎片 —— 它们没有作者，查了也是白查。
    skip = NOT_AUTHOR | {"正文", "讲义", "作品相关", "简介", "目录", "序",
                         "前言", "后记", "附录", "尾声", "楔子", "番外"}
    pending = [{"title": t, "files": n, "state": "todo"}
               for t, n in cnt.most_common()
               if (t not in lib or lib[t]["source"] == "local-weak")
               and t not in skip and len(t) >= 2]
    with open(PENDING_JSON, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=0)
    print(f"书名总数 {len(cnt)}，已有本地作者 {len(lib)}，待联网 {len(pending)}")
    print(f"写入 {PENDING_JSON}")
    print("前 30 个高频待查：")
    for p in pending[:30]:
        print(f"  {p['title']}\t{p['files']}")


def stat():
    cnt = _all_titles()
    lib = {}
    if os.path.exists(AUTHORS_JSON):
        with open(AUTHORS_JSON, encoding="utf-8") as f:
            lib = json.load(f)
    by_src = collections.Counter(v["source"] for v in lib.values())
    covered_files = sum(n for t, n in cnt.items() if t in lib)
    print(f"书名总数 {len(cnt)}　文件总数 {sum(cnt.values())}")
    print(f"作者库 {len(lib)} 本　按来源：{dict(by_src)}")
    print(f"已覆盖文件 {covered_files} 条（{covered_files / max(1, sum(cnt.values())):.1%}）")
    missing = [(t, n) for t, n in cnt.items() if t not in lib]
    print(f"仍缺作者的书名 {len(missing)}，涉及文件 {sum(n for _, n in missing)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"build": build, "export": export, "stat": stat}[cmd]()
