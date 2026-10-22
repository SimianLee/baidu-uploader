# -*- coding: utf-8 -*-
"""非小说判定：学术专著 / 电脑教程 / 期刊栏目 / 站点公告 / 术数古籍。

为什么要单独一个模块，而不是往 novel_clean.FAMILIES 里加词：
FAMILIES 是「书名里出现这个词就判死」的黑名单，加错一个词会连带杀掉
上万本真小说（`读者` 进了期刊表，`读者文摘` 时期的言情书名全中枪）。
这里换一套判据：**不看单个词，看整体形状**。一条记录只有满足
「书名长度 + 结构词 + 副标题」这类组合特征时才判为非小说。

用法：
    python tools/nonfiction.py scan    # 全库扫描，输出清单与统计，不改表
    python tools/nonfiction.py sample  # 只吐 120 条样本给人眼看
"""
import collections
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8")
import novel_clean as nc                        # noqa: E402

CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# ---- 明确是期刊栏目 / 站点公告的整名（不是书，也不该联网查作者） ----
PERIODICAL = {
    "财经", "哈佛商业评论", "中国全史", "长篇小说的选刊", "原创天地",
    "专家坐堂", "追新一族", "城市硬件DIY情报站", "软件一点通",
    "驱动程序更新快报", "硬件瞭望台", "玖头在线", "醉逍遥书屋",
    "第一", "VIP会员必读说明",
}
PERIODICAL_PAT = re.compile(
    r"(必读说明|会员必读|更新快报|驱动程序|硬件瞭望|情报站|"
    r"投稿须知|征稿启事|免责声明|站务|版规)")

# ---- 学术/教材的结构词：出现在书名里，基本可以确定不是小说 ----
ACADEMIC = (
    "概论", "导论", "通论", "引论", "绪论", "讲义", "教程", "读本", "教本",
    "原理与应用", "原理及应用", "基础教程", "实用教程", "入门与提高",
    "研究", "探析", "述评", "综述", "论纲", "新论", "初探", "刍议",
    "问题与对策", "案例分析", "实证研究", "理论前沿", "学科建设",
    "第X版", "修订版", "增订本", "影印本", "校注本",
)
# 注意「全史」「通史」「简史」不在上面：那是畅销书常用名（《人类简史》），
# 只在「长度够长 + 还带别的结构词」时才一起算，见 score()。

# ---- 电脑/数码教程的结构词 ----
COMPUTER = (
    "教程", "从入门到精通", "入门到精通", "实战指南", "完全手册",
    "技术手册", "开发指南", "编程思想", "权威指南", "最佳实践",
    "第X版", "疑难解析", "应用实例", "上机指导", "实验指导",
)

# ---- 非叙事文体的标记 ----
NON_NARRATIVE = (
    "报告", "白皮书", "蓝皮书", "年鉴", "年报", "公报", "汇编",
    "论文集", "文集", "演讲集", "访谈录", "口述史", "回忆录",
    "日记", "书信集", "文稿", "手稿", "笔记", "札记", "题跋",
)

# ---- 术数/方技/经部古籍的定式名称 ----
DIVINATION_TAIL = ("发微", "钩隐", "秘解", "直解", "阐微", "探源", "考",
                   "注", "疏", "正义", "集解", "通释", "纂要", "节要")


def _flat(s):
    return re.sub(r"[\s\-_·.。，、（）()\[\]【】《》:：;；!！?？]", "", s)


def _known_nonfiction(stem):
    """整名命中的：期刊栏目、站点公告"""
    if stem in PERIODICAL:
        return "期刊栏目/站内公告"
    if PERIODICAL_PAT.search(stem):
        return "期刊栏目/站内公告"
    return None


def score(stem):
    """给一条文件名打分，返回 (分数, 命中的理由列表)

    分数 ≥3 判为非小说。之所以要 3 分而不是 1 分：
    `心理学` 单独出现可能是小说名，但 `心理学导论（第3版）` 一定不是。
    """
    t = _flat(stem)
    if not t:
        return 0, []
    hits = []
    n = len(t)

    known = _known_nonfiction(stem)
    if known:
        hits.append(known)

    # 术数/古籍：`皇极经世心易发微`（明·杨向春，八卷，方技家言）
    if n >= 6 and t.endswith(DIVINATION_TAIL):
        hits.append("古籍术数定式")
    if re.search(r"(经世|皇极|太玄|滴天髓|渊海子平|三命通会|麻衣相法|"
                 r"穷通宝鉴|梅花易数|奇门遁甲|六壬|太乙)", t):
        hits.append("术数类书名")

    # 学术专著：结构词 + 长度够
    if any(w.replace("X", "") in t for w in ACADEMIC if w != "第X版") and n >= 4:
        hits.append("学术结构词")
    if n >= 8 and re.search(r"(全史|通史|简史|史话|史纲)$", t):
        hits.append("史学通名+长标题")

    # 电脑教程
    if any(w.replace("X", "") in t for w in COMPUTER if w != "第X版") and n >= 3:
        hits.append("电脑教程结构词")
    if re.search(r"(局域网|显卡|主板|CPU|内存|硬盘|光驱|超频|装机|"
                 r"BIOS|注册表|系统优化|病毒防治|操作系统安装)", t):
        hits.append("硬件/系统标题")

    # 非叙事文体
    if any(w in t for w in NON_NARRATIVE) and n >= 5:
        hits.append("非叙事文体")

    # 副标题结构：`主标题：副标题` 且两边都长（≥6 + ≥5 字）
    m = re.match(r"^(.{6,})[：:](.{5,})$", stem)
    if m and len(CJK_RUN.findall(m.group(1))) and \
            len(CJK_RUN.findall(m.group(2))):
        hits.append("长主副标题")

    # 极度冗长的标题（≥18 汉字）：小说名极少这么长
    if len(CJK_RUN.findall(stem)) >= 18:
        hits.append("超长标题")

    return len(hits), hits


def is_nonfiction(stem):
    """主流程调用：返回 (是否非小说, 理由串)"""
    s, hits = score(stem)
    # 阈值 3：单一结构词（`研究`、`文集`）不够 —— 很多小说也叫《局外人研究》。
    # 至少三条同时命中，或者整名就是期刊栏目，才判死。
    if s >= 3 or (hits and hits[0] == "期刊栏目/站内公告"):
        return True, "非小说(" + "+".join(hits[:3]) + ")"
    return False, ""


def scan(rows=None):
    if rows is None:
        rows, _ = nc.load_rows()
    out = []
    for r in rows:
        name = r["文件名"]
        ext = (r.get("后缀") or "").strip()
        bad, why = nc.classify(name, ext)
        if bad:
            continue                              # 已经被现有规则判死的不管
        stem = nc.base_stem(name, ext)
        s, hits = score(stem)
        if s >= 3:
            out.append((s, hits, name))
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sample"
    res = scan()
    print(f"疑似非小说：{len(res)} 条 / 全库 {516685}")
    c = collections.Counter()
    for s, hits, name in res:
        for h in hits:
            c[h] += 1
    print("\n命中分布：")
    for k, v in c.most_common():
        print(f"  {k:<20} {v}")
    if cmd == "sample":
        print("\n=== 样本 ===")
        import random
        random.seed(7)
        for s, hits, name in random.sample(res, min(120, len(res))):
            print(f"  [{s}] {name}   <- {'/'.join(hits)}")


if __name__ == "__main__":
    main()
