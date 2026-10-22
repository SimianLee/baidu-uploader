# -*- coding: utf-8 -*-
"""第五轮：后处理补丁（不重跑全库 classify，只读 CSV 局部改写）

处理验收脚本没覆盖到的四类漏网：
  1. 版本词 / 栏目词 / 国别朝代 占了作者位   -> 清掉作者位
  2. 漏网的非小说（技术书 / 考试书 / 旅行指南 / 健康美容）-> 删
  3. 广告 / 网址残留                        -> 删
  4. 序号点、出版社译者尾巴、半角标点        -> 剥掉 / 转全角

用法：
    python tools/patch5.py          # 试跑，只打印统计，不写盘
    python tools/patch5.py --write  # 真写回 CSV
"""
import csv, re, os, sys, shutil, collections

CSV = r"C:\Users\UserData\Desktop\文件清单_apps_baidu_uploader_归档_完整.csv"
CJK = re.compile(r"[\u4e00-\u9fff]")
STEM = re.compile(r"^([^《》]*)《([^》]*)》(.*)$")

# ---------------------------------------------------------------- 删除判定

# 强广告：站点水印、网址、收集整理
AD = re.compile(r"(www\.|\.com|\.net|\.org|华文网|收集整理|网收集|"
                r"快眼看书|啃书网|电子书下载|txt下载|免费下载|"
                r"欢迎访问|点击进入|版权归|免责声明)")

# 非小说（与 _residue.py 口径一致，用户已确认「全部都删」）
NF = re.compile(r"(教程|指南|手册|大全|全书|辞典|词典|字典|宝典|技法|实战|"
                r"从入门|入门到|精通|实例|案例精|Office|WPS|Excel|Word|"
                r"Photoshop|股票|基金|理财|投资指南|养生|健康|美容|减肥|"
                r"食谱|菜谱|瑜伽|孕产|育儿|带兵|管理学|营销|销售|职场|"
                r"面试|演讲|口才|沟通技巧|回忆录|自传|年谱|百科全书|"
                r"申请指南|租房|买房)")

# ---------------------------------------------------------------- 作者位清洗

NATION = re.compile(r"^(英国|美國|美国|法国|法國|德国|德國|日本|俄国|俄國|苏联|蘇聯|"
                    r"古希腊|古希臘|古罗马|古羅馬|意大利|義大利|印度|中国|中國|"
                    r"台湾|臺灣|香港|韓国|韩国|西班牙|加拿大|澳大利亚|澳門|澳门|"
                    r"荷兰|荷蘭|波蘭|波兰|瑞士|瑞典|挪威|丹麥|丹麦|爱尔兰|"
                    r"奧地利|奥地利|匈牙利|捷克|希腊|希臘|羅馬|罗马|埃及|以色列|"
                    r"阿根廷|巴西|墨西哥|古巴|朝鮮|朝鲜|越南|泰国|泰國|新加坡|"
                    r"馬來|马来|尼泊尔|蒙古|波斯|阿拉伯|犹太|猶太|欧美|歐美|"
                    r"外国|外國|海外|近代|现代|現代|当代|當代|古代|民国|民囯|"
                    r"清代|明代|宋代|唐代|汉代|漢代|先秦|魏晋|魏晉|南北朝|"
                    r"隋唐|宋元|明清|西汉|東漢|东汉|南宋|北宋|晚清|元明)$")

# 版本 / 栏目 / 装帧 / 状态词
VER = re.compile(r"(完整|扫描|掃描|翻譯|翻译|译$|编著|主編|主编|编译|編譯|"
                 r"整理|收集|大全|全书|全集|合集|系列|丛书|叢書|文集|选集|"
                 r"版$|寸版|出品|出版|下载|下載|修订|校订|校对|排版|制作|"
                 r"独家|首發|首发|精校|完本|完结|完結|连载|連載|推荐|推薦|"
                 r"热门|熱門|精品|经典|經典|名著|文库|文庫|典藏|书库|書庫|"
                 r"专题|專題|官方|作品|之选|之選|必读|必讀|套装|套裝|"
                 r"全文|正文|书系|書系|系列文|小说卷|小說卷|导读|導讀)")

# 出版社牌号（当作者的一律清掉）
PUB = re.compile(r"^(理想国|三聯|三联|中华书局|中囯书局|商务印书馆|人民文学|"
                 r"上海译文|上海譯文|译林|譯林|中信|新星|磨铁|磨鐵|读客|讀客|"
                 r"果麦|果麥|博集天卷|湛庐|湛廬|世纪文景|燕山|百花|长江文艺|"
                 r"長江文藝|作家出版|江苏文艺|浙江文艺|湖南文艺|春风文艺|"
                 r"群众出版|解放军文艺|外文出版|中国青年|少年儿童|接力|"
                 r"明天出版|二十一世纪|广西师大|北京十月|十月文艺|四川文艺|"
                 r"山东文艺|安徽文艺|河南文艺|河北教育|山东画报|吉林出版|"
                 r"北方文艺|大众文艺|知识出版|科学出版|人民出版社)$")

# ---------------------------------------------------------------- 标题清洗

CN_NUM = "一二三四五六七八九十百千万零两"

# 「十一.如何实现…」「4.X型题」：序号 + 点/顿号
ORD_PREFIX = re.compile(r"^\s*(?:\d{1,3}|[" + CN_NUM + r"]{1,4})\s*[.、·]\s*")

# 出版社 / 译者 / 尺寸 尾巴（按点分段，从命中段起全砍）
PUB_SEG = re.compile(r"(译|譯|出版社|出版公司|寸版|扫描|掃描|收集整理)")

# 《鬼吹灯之圣泉寻踪.umd》：上一轮把扩展名留在了书名里
EXT_IN = re.compile(r"[.。]\s*(txt|pdf|epub|mobi|umd|azw3?|doc|docx|html?|chm|"
                    r"rar|zip|djvu|pptx?|xlsx?|cbz|cbr|apk|exe|jpe?g|png)$", re.I)

# 《孤独与沉思.六》：尾巴上的寸版号（6 寸版被转成「六」）
TAIL_VOL = re.compile(r"[.。]\s*(?:\d{1,2}|[" + CN_NUM + r"]{1,3})\s*$")

# 《%九十一陋的韩国人》：URL 百分号编码被转成了中文数字
PCT = re.compile(r"[%％]\s*[\dA-Fa-f" + CN_NUM + r"]{1,5}")

FULL_MAP = {
    ",": "，", ";": "；", "!": "！", "?": "？", "~": "～",
    "(": "（", ")": "）", ":": "：",
}
PROTECT_L = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
PROTECT_R = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def to_fullwidth(s):
    """半角 -> 全角。

    例外：夹在 ASCII 字母/数字之间的点要留着 —— `F.S.菲茨杰拉德`
    是人名缩写、`Node.js` 是项目名，转成 `F。S。` 就毁了。
    """
    out = []
    for i, ch in enumerate(s):
        if ch == ".":
            l = s[i - 1] if i > 0 else ""
            r = s[i + 1] if i + 1 < len(s) else ""
            if l in PROTECT_L and r in PROTECT_R:
                out.append(".")
            else:
                out.append("。")
        else:
            out.append(FULL_MAP.get(ch, ch))
    return "".join(out)


def strip_pub_tail(t):
    """`桑塔格短篇小说选.申惠辉等译.南海出版公司` -> `桑塔格短篇小说选`

    按点切段，从第一段落到「译/出版社/寸版」的段开始全砍。
    砍完剩不下 2 个汉字就说明切错了（如 `译文序跋集.鲁迅.六`），原样退回。
    """
    if not re.search(r"[.。·]", t):
        return t
    segs = re.split(r"[.。·]", t)
    keep = []
    for sg in segs:
        if PUB_SEG.search(sg):
            break
        keep.append(sg)
    head = ".".join(keep).strip()
    if len(CJK.findall(head)) >= 2:
        return head
    return t


def clean_author(a):
    """作者位清洗。返回 '' 表示这位置上的东西不是人名，直接去掉。"""
    a = (a or "").strip()
    if not a:
        return ""
    if NATION.match(a) or PUB.match(a) or VER.search(a):
        return ""
    # `周贵华译` -> `周贵华`（真译者留着）；`张译` -> 剩 1 字，不像人名，丢
    if a.endswith("译") or a.endswith("譯"):
        a = a[:-1]
        if not (2 <= len(CJK.findall(a)) <= 4):
            return ""
    if len(CJK.findall(a)) == 0 or len(a) >= 8:
        return ""
    return a


def clean_title(t):
    orig = t
    t = EXT_IN.sub("", t)
    t = PCT.sub("", t)
    t = strip_pub_tail(t)

    m = ORD_PREFIX.match(t)
    if m:
        rest = t[m.end():]
        # 剥完要是还以数字打头（`一.二.三.木头人` -> `二.三.木头人`），
        # 说明那不是序号而是书名的一部分，别剥。
        if len(CJK.findall(rest)) >= 2 and rest[0] not in CN_NUM and not rest[0].isdigit():
            t = rest

    t = TAIL_VOL.sub("", t)
    t = t.replace("=", " ").replace("＝", " ")
    t = re.sub(r"\s{2,}", " ", t)
    t = to_fullwidth(t).strip(" 。·、-")

    # 洗过头了就退回原样 —— 宁可留点脏，也不能把书名洗没
    if len(CJK.findall(t)) < 2:
        return orig
    return t


# ---------------------------------------------------------------- 主流程

def main():
    write = "--write" in sys.argv
    rows = list(csv.reader(open(CSV, encoding="utf-8-sig", newline="")))
    hdr = rows[0]
    i_op, i_new, i_old = hdr.index("操作"), hdr.index("新文件名"), hdr.index("文件名")
    i_ext, i_size = hdr.index("后缀"), hdr.index("大小(字节)")

    stat = collections.Counter()
    del_sample = {"广告": [], "非小说": [], "去重": []}
    fix_sample = []

    for r in rows[1:]:
        if r[i_op].strip() == "删除" or not r[i_new].strip():
            continue
        stem, ext = os.path.splitext(r[i_new])
        m = STEM.match(stem)
        if not m:
            continue
        author, title, vol = m.group(1), m.group(2), m.group(3)

        # --- 1) 删除判定（在清洗之前判，别让清洗把证据洗没了）
        whole = title + " " + author
        if AD.search(r[i_new]):
            r[i_op], r[i_new] = "删除", ""
            stat["删·广告"] += 1
            del_sample["广告"].append(r[i_old])
            continue
        if NF.search(title):
            r[i_op], r[i_new] = "删除", ""
            stat["删·非小说"] += 1
            if len(del_sample["非小说"]) < 60:
                del_sample["非小说"].append(r[i_new] or r[i_old])
            continue

        # --- 2) 清洗
        na, nt = clean_author(author), clean_title(title)
        new = ("%s《%s》%s" % (na, nt, vol)) + ext
        if new != r[i_new]:
            if na != author:
                stat["修·作者位"] += 1
            if nt != title:
                stat["修·标题"] += 1
                if len(fix_sample) < 60:
                    fix_sample.append((r[i_new], new))
            r[i_new] = new
            r[i_op] = "改名"

    # --- 3) 去重：清洗后可能撞名（`《四.X型题》` 剥完变 `《X型题》`）
    by = collections.defaultdict(list)
    for r in rows[1:]:
        if r[i_op].strip() != "删除" and r[i_new].strip():
            by[r[i_new].strip()].append(r)
    for k, g in by.items():
        if len(g) < 2:
            continue
        g.sort(key=lambda r: -(int(r[i_size] or 0)))
        for extra in g[1:]:
            extra[i_op], extra[i_new] = "删除", ""
            stat["删·去重"] += 1
            if len(del_sample["去重"]) < 30:
                del_sample["去重"].append(k)

    # --- 4) 汇总
    keep = sum(1 for r in rows[1:] if r[i_op].strip() != "删除" and r[i_new].strip())
    print("保留:", keep, " 删除:", len(rows) - 1 - keep)
    print()
    for k, v in stat.most_common():
        print(f"  {k}\t{v}")
    print("\n=== 删·非小说 样本 ===")
    for x in del_sample["非小说"][:40]:
        print("  ", x)
    print("\n=== 删·广告 样本 ===")
    for x in del_sample["广告"][:30]:
        print("  ", x)
    print("\n=== 标题修正样本 ===")
    for a, b in fix_sample[:40]:
        print(f"  {a}\t->\t{b}")

    if not write:
        print("\n[试跑] 加 --write 才写盘")
        return

    if not os.path.exists(CSV + ".bak5"):
        shutil.copy2(CSV, CSV + ".bak5")
        print("\n已备份:", CSV + ".bak5")
    with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows[1:])
    print("已写回:", CSV)


if __name__ == "__main__":
    main()
