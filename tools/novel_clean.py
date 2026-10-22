# -*- coding: utf-8 -*-
"""把归档书库里的小说挑出来、改名，结果直接写回导出的那份 CSV。

判定分两层，原则是「宁可放过，不可错杀」：
  1. 硬命中：出现一个就判死（教案/题库/课件/伤寒论/半月谈/绘本…）；
  2. 软命中：单个只算「嫌疑」，跨两个以上大类才判死——
     因为「智慧」「人生」「历史」这类词偶尔也会出现在书名里。

改名：脱括号噪声 → 去版本/来源尾巴 → 切段 → 扔掉作者名段 → 留第一段；
      卷号：上/下 → -上部/-下部；第X卷/第X部/第X季 → -第X卷；
      「裸上 / 裸下」只有当库里存在同名兄弟卷时才认，避免「天下」「人生边上」被切；
      繁体书名**不删**，统一转成简体（to_simplified）。

用法：
    python tools/novel_clean.py summary       # 只统计、吐样本，不动原表
    python tools/novel_clean.py apply         # 写回 CSV（先备份成 .bak）
"""
import collections
import csv
import html
import os
import random
import re
import shutil
import sys
from functools import lru_cache

csv.field_size_limit(10 ** 9)

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.environ.get("DESKTOP_DIR") or r"C:\Users\UserData\Desktop"
# 默认吃「修好扫描 bug 之后重导的完整清单」（516685 条，含此前丢失的 txt 目录）
CSV_NAME = os.environ.get("CSV_NAME") or \
    "文件清单_apps_baidu_uploader_归档_完整.csv"
CSV_IN = os.path.join(DESKTOP, CSV_NAME)
OUT = os.path.join(HERE, "_out")
os.makedirs(OUT, exist_ok=True)

# txt 必须在这里：这次的清单是修好「大目录静默丢失」之后重导的完整版，
# /归档/txt 一个目录就有 40 万条，占了全库近八成。旧清单里根本没有 txt 目录，
# 所以这张表当初没考虑它 —— 沿用旧表会把 40 万部小说全判成「非书格式」删掉。
KEEP_FMT = {"epub", "mobi", "azw3", "pdf", "txt", "umd", "docx", "doc", "chm"}
FMT_RANK = {e: i for i, e in enumerate(
    ["epub", "mobi", "azw3", "pdf", "txt", "umd", "docx", "doc", "chm"])}

# ------------------------------------------------------------ 语言判定
try:
    import opencc
    _CC = opencc.OpenCC("t2s")
except Exception:                                     # pragma: no cover
    _CC = None

CJK = re.compile(r"[\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff]")
HANGUL = re.compile(r"[\uac00-\ud7af]")
JP_ONLY = set("図価沢辻込働畑匂凧駅兎")            # 只在日语里用的汉字


# 混在书名里的格式噪声，不算「外文」
NOISE_LATIN = {"pdf", "epub", "mobi", "azw", "azw3", "doc", "docx", "chm",
               "txt", "umd", "vol", "nodrm", "vip", "www", "com", "net",
               "cn", "html", "htm", "zip", "rar", "iso", "jpg", "jpeg",
               "png", "gif", "kindle", "prc", "djvu"}


@lru_cache(maxsize=400000)
def to_simplified(s):
    """繁体 → 简体（带缓存：51 万条里书名重复不少，且同一条要被判/改两次）

    这次的要求是「繁体不删，改成简体」，所以繁体不再算要删的语言，而是留到
    改名这一步统一转。对本来就是简体的文本，t2s 不会动它（不在转换表里的字
    原样返回），所以可以无条件调用、不必先判是不是繁体。
    """
    if _CC is None:
        return s
    try:
        return _CC.convert(s)
    except Exception:
        return s


def lang_of(text):
    """none=简体中文；繁体=要转成简体（不删）；其余是要删的语言"""
    if KANA.search(text) or any(c in JP_ONLY for c in text):
        return "日文"
    if HANGUL.search(text):
        return "韩文"
    cjk = len(CJK.findall(text))
    if cjk == 0:
        return "纯外文"
    if _CC is not None:
        try:
            if _CC.convert(text) != text:
                return "繁体"
        except Exception:
            pass
    words = [w for w in re.findall(r"[A-Za-z]{2,}", text)
             if w.lower() not in NOISE_LATIN]
    lat = sum(len(w) for w in words)
    if (lat >= 8 and lat > cjk) or (lat >= 4 and cjk <= 3):
        return "外文为主"
    return "none"


# 数字后面跟这些字，说明数字是书名的一部分（30天 / 60个瞬间 / 5分钟）
MEASURE = set("天年月日个分秒周小本部队卷章节篇条种次位名人岁块元万亿千百十套只间句字页例味道")

# 「X学」结尾的豁免：这些真不是学科专著
XUE_EXCEPT = ["大学", "小学", "中学", "上学", "放学", "同学", "留学",
              "入学", "办学", "失学", "休学", "退学", "转学", "开学",
              "游学", "私学", "太学", "国学"]

# ---------------------------------------------- 非小说大类（hard / soft）
FAMILIES = {
    "垃圾/无效名": {
        "hard": [],
        "soft": [],
        "re": [
            r"^(打印|新建|副本|复件|未命名|无标题|缺书|书单说明|文档|新建文档)"
            r"\s*\d*$",
            r"^第\s*[0-9一二三四五六七八九十百]{1,4}\s*[章节讲篇]\s*[0-9A-Za-z]*$",
            r"^第\s*[0-9一二三四五六七八九十百]{1,4}\s*[卷部册集]\s*[A-Za-z]?$",
            r"^contents\s+of",
            r"^\d{1,4}$",
            r"^第?\d{2,4}[-—–_]\d{2,4}\s*(卷|部|集|册|话|話)$",
            # `★本文件夹所有TXT文件列表★_by完结小说网 (27)`：这是书库的索引文件，
            # 改完名会撞成同一个名字，去重后只剩一本，留着没意义。
            r"文件列表",
            # 百度知道的问答页，不是书
            r"百度知道",
            # `B 小说类 恐怖 (10)`：只有「分类 + 编号」，书名根本不在文件名里。
            # 这类是书库按类导出的条目，认不出书名，删比留好。
            r"^[A-Za-z]?\s*[\u4e00-\u9fff]{2,4}类\s*[\u4e00-\u9fff]{0,6}"
            r"\s*[（(]?\d{1,4}[)）]?\s*$",
        ],
    },
    "教辅考试": {
        "hard": [
            "教材", "课本", "教科书", "教案", "说课", "讲义", "讲稿", "备课",
            "教学大纲", "课程标准", "课标", "同步训练", "同步辅导", "同步练习",
            "习题集", "习题精解", "习题解答", "课后答案", "试题", "题库",
            "试卷", "统考", "模拟卷", "真题", "竞赛辅导", "九年制", "义务教育",
            "学前班", "幼儿园", "小学", "初中", "高中", "小升初", "中考",
            "高考", "考研", "期末", "期中", "月考", "单元考", "四六级",
            "新概念英语", "剑桥少儿", "背单词", "词汇速记", "语法新思维",
            "语法手册", "英语语法", "日语学习", "标日", "法语入门", "德语入门",
            "拼音", "生字", "必考点", "考点解析", "过关必做", "特级教师",
            "名师", "随堂", "测验", "教学设计", "教学案例", "说课稿",
            "精讲精练", "一课一练", "训练册", "练习册", "单元测试",
            "学位论文", "学术论文", "开题报告", "毕业论文", "博士论文",
            "硕士论文", "读后感", "观后感", "满分作文", "作文素材", "范文",
            "好词", "好句", "好段", "名人名言", "格言", "警句", "歇后语",
            "成语故事", "俗语", "谚语", "谜语", "对联", "口号", "标语",
            "台词", "自我介绍", "自荐信", "求职信", "述职报告", "工作总结",
            "公文写作", "应用文", "策划方案", "会议纪要", "心得体会",
            "汇报材料", "事迹材料", "演讲词", "主持词", "发言稿", "合同条款",
            "规章制度", "管理制度", "操作规范", "学生", "课堂", "课程",
            "教程", "大纲", "考点", "训练", "习题", "教师",
        ],
        "soft": ["学习方法", "教育", "学习", "考试", "培训", "训练", "教材",
                 "习题", "课程", "课堂", "教学", "教师", "辅导"],
    },
    "计算机IT": {
        "hard": [
            "Linux", "linux", "Shell脚本", "Windows教程", "Excel", "excel",
            "PowerPoint", "Photoshop", "AutoCAD", "MATLAB", "Oracle",
            "MySQL", "Python", "python", "Java", "java", "C++", "C#",
            "JavaScript", "HTML", "CSS3", "PHP", "SQL Server", "Android",
            "iOS开发", "TensorFlow", "数据结构", "操作系统", "编译原理",
            "数据库", "网络协议", "单片机", "嵌入式", "路由器", "交换机",
            "编程", "程序员", "源代码", "软件工程", "软件测试", "命令行",
            "黑客", "机器学习", "大数据", "云计算", "区块链", "深度学习",
            "数据挖掘", "爬虫", "前端开发", "Bootstrap", "jQuery",
            "WordPress", "虚拟机", "服务器配置", "注册表", "批处理",
            "电脑入门", "网页制作", "动画制作", "视频剪辑", "ABAP", "SAP",
        ],
        "soft": ["算法", "代码", "网站", "系统", "技术", "网络", "软件",
                 "电脑", "数据", "数码"],
    },
    "医学养生": {
        "hard": [
            "医学", "中医", "西医", "本草", "黄帝内经", "伤寒", "金匮",
            "温病", "方剂", "汤头歌", "穴位", "经络", "针灸", "推拿",
            "按摩", "拔罐", "刮痧", "气功", "丹道", "筑基", "内丹",
            "临床", "病例", "病历", "诊断", "疗法", "内科", "外科",
            "儿科", "妇科", "骨科", "药学", "处方", "舌诊", "脉诊",
            "医案", "医话", "养生", "食疗", "食补", "膳食", "营养学",
            "减肥", "瘦身", "增肌", "瑜伽", "八段锦", "五禽戏", "保健",
            "护理学", "解剖", "寄生虫", "微生物", "流行病", "急救",
            "偏方", "秘方", "验方", "老偏方", "中草药", "药材", "中药",
            "妊娠", "孕妇", "育儿", "胎教", "坐月子", "辅食", "早教",
            "美容", "护肤", "化妆", "祛斑", "丰胸", "美发", "穿搭",
            "排毒", "便秘", "失眠", "颈椎", "腰椎", "高血压",
            "糖尿病", "冠心病", "妇科病", "不孕", "壮阳", "补肾",
            "调理", "药性", "汤剂", "脉象", "病症", "医疗",
            "良方", "证治", "歌括", "膏药", "汤液", "经方", "脉诀",
            "素问", "灵枢", "难经", "医宗", "经注", "诊治", "防治",
            "病源", "汤头", "药材", "本草", "验方", "偏方", "秘方",
        ],
        "soft": ["健康", "身体", "饮食", "疾病", "症状"],
    },
    "命理宗教古籍": {
        "hard": [
            "周易", "易经", "易学", "八卦", "六爻", "梅花易数", "奇门遁甲",
            "紫微斗数", "风水", "面相", "手相", "骨相", "姓名学", "八字",
            "命理", "择日", "黄历", "通书", "阴宅", "阳宅", "罗经", "河图",
            "洛书", "太玄", "星相", "占卜", "塔罗", "星座运势", "血型",
            "滴天髓", "子平", "渊海子平", "三命通会", "穷通宝鉴", "麻衣",
            "柳庄", "冰鉴", "六壬", "太乙神数", "铁板神数", "择吉",
            # 注意：不能只写「佛」，否则「哈佛」被误杀
            "佛教", "佛学", "佛法", "佛经", "佛陀", "佛祖", "菩萨", "般若",
            "金刚经", "法华经", "华严经", "药师经", "菩提", "阿弥陀",
            "净土", "往生", "戒律", "弘法", "因果实录", "弟子规",
            "感应篇", "了凡四训", "太上感应", "圣经", "祷告", "灵修",
            "古兰经", "楞严经", "心经", "坛经", "无量寿", "地藏经",
            "道德经", "论语", "中庸", "尚书", "礼记", "左传", "国语",
            "战国策", "史记", "汉书", "后汉书", "三国志", "资治通鉴",
            "纲鉴", "菜根谭", "围炉夜话", "呻吟语", "传习录", "四书五经",
            "千字文", "三字经", "增广贤文", "幼学琼林", "声律启蒙",
            "诗话", "词话", "诗品", "文心雕龙", "人间词话", "太极拳",
            "符咒", "咒语", "开光", "轮回", "前世", "因果", "修道",
            # 道藏 / 大藏经：整库一进来就是上千个 `正统道藏XX部-XX经-朝代-作者`，
            # 全是宗教典籍，不是小说。不认出来它们会全部撞成「正统道藏正一部」
            # 这一个名字，去重后只剩一本。
            "道藏", "大藏经", "藏经", "续藏", "佛藏", "敦煌", "大正藏",
            "中国撰述", "撰述部", "史传部", "本缘部", "阿含部",
            "般若部", "法华部", "华严部", "宝积部", "涅槃部", "论集部",
            "经集部", "密教部", "律部", "论部", "事汇部", "古逸部",
            "疑似部", "目录部", "外教部",
        ],
        "soft": ["禅", "国学", "儒家", "道家", "佛教", "基督", "经文",
                 "修行", "养心"],
    },
    "期刊杂志": {
        "hard": [
            "半月谈", "人民日报", "求是", "光明日报", "参考消息",
            "环球时报", "南方周末", "三联生活周刊", "三联周刊", "新周刊",
            "财经杂志", "商界", "读者文摘", "青年文摘", "格言杂志",
            "特别关注", "杂文选刊", "散文百家", "故事会", "科幻世界",
            "九州幻想", "萌芽杂志", "译林", "人民文学杂志", "小说月报",
            "中篇小说选刊", "长篇小说选刊", "今古传奇", "章回小说",
            # 曲艺/戏剧脚本：不是小说（刘兰芳、单田芳这些评书艺人的文件全是这个）
            "评书", "相声", "快板书", "大鼓", "二人转", "剧本", "话剧",
            "广播剧", "鼓书", "琴书", "评话",
            "兵器知识", "环球科学", "国家地理", "周刊", "月刊", "双月刊",
            "季刊", "合订本", "增刊", "杂志", "年第", "第01期", "第1期",
            "第2期", "第3期", "第4期", "第5期", "第6期", "第7期", "第8期",
            "第9期", "第10期", "第11期", "第12期", "全年精选", "合刊",
        ],
        "soft": [],
    },
    "工具书素材": {
        "hard": [
            "词典", "字典", "辞典", "辞海", "名录", "年鉴", "手册",
            "索引", "书目", "速查", "便查", "工具书", "资料汇编",
            "资料集", "汇编", "摘编", "成语", "四字词语", "形容词",
            "起名", "取名", "命名", "祝福语", "笑话", "脑筋急转弯",
            "谜语", "名言", "词汇", "谚语",
            # 丛书/书系名：`诺贝尔文学奖作品典藏书系全集-S.拉格洛芙`。
            # 真正的书名不在这类文件名里，留下来是一堆同名废条目。
            "书系", "诺贝尔文学奖", "作品集", "文集", "选集", "文粹",
        ],
        "soft": ["大全", "宝典", "百科", "素材", "写作"],
    },
    "学术社科": {
        "hard": [
            "哲学", "伦理学", "逻辑学", "美学", "宗教学", "社会学",
            "人类学", "政治学", "经济学", "金融学", "会计学", "统计学",
            "财政学", "市场营销", "管理学", "组织行为", "战略管理",
            "教育学", "法学", "法制史", "宪法", "民法", "刑法",
            "行政法", "国际法", "劳动法", "公司法", "合同法", "税法",
            "知识产权", "国际关系", "军事理论", "战争论", "军事思想",
            "党史", "党建", "社会主义", "三个代表", "科学发展观",
            "邓小平理论", "马克思主义", "毛泽东", "思想研究",
            "研究丛书", "论文集", "演讲集", "访谈录", "研究文集",
            "课题", "纪实文学", "回忆录", "自传", "年谱", "家谱",
            "族谱", "地方志", "报告文学", "白皮书", "心理学",
            "史学", "概论", "导论", "纲要", "原理", "方法论",
            "通论", "简史", "通史", "史话", "纪实", "专著",
            "发展史", "思想史", "经济史", "战争史", "哲学史",
            "美学史", "文学史", "简论", "论集", "述评", "综述",
            "调查报告", "制度", "治理", "改革", "思潮", "文库",
            "丛书", "读本", "讲坛",
        ],
        "soft": ["研究", "文化", "艺术", "历史", "社会", "心理", "教育",
                 "法律", "军事", "政治", "经济", "经典", "名著", "理论",
                 "主义", "革命", "逻辑", "思维", "思想", "文明", "制度",
                 "治理", "改革", "发展", "分析", "解读", "研究", "报告",
                 "国家", "世界", "全球", "国际", "现代", "当代", "古代",
                 "民国", "世纪", "时代", "帝国", "中华", "哲学", "科学",
                 "技术", "概论", "导论", "纲要", "原理", "方法论", "简史",
                 "通史", "史话", "纪实", "专著", "课题", "综述", "述评"],
    },
    "经管励志": {
        "hard": [
            "励志", "正能量", "心灵鸡汤", "致富", "财商", "穷爸爸",
            "富爸爸", "卡耐基", "成功", "羊皮卷", "人性的弱点",
            "商业模式", "创业", "职业规划", "职场", "团队精神",
            "领导力", "执行力", "时间管理", "项目管理", "供应链",
            "物流管理", "人力资源", "质量管理", "生产管理",
            "绩效考核", "企业文化", "品牌建设", "市场营销",
            "营销策划", "市场调研", "商业计划书", "可行性研究",
            "招投标", "谈判技巧", "演讲技巧", "口才训练", "说服力",
            "影响力", "沟通技巧", "社交礼仪", "商务礼仪", "职场礼仪",
            "情绪管理", "自控力", "自律", "拖延", "专注力", "理财规划",
            "投资理财", "股票投资", "基金投资", "期货交易", "外汇交易",
            "保险理财", "个人理财", "处世", "为人处世", "说话技巧",
            "说话的艺术", "气场", "格局", "境界", "修养", "修身",
        ],
        "soft": ["管理", "经营", "战略", "商业", "投资", "指南", "攻略",
                 "入门", "精通", "实战", "案例", "技巧", "方法", "效率",
                 "习惯", "沟通", "认知", "理财", "成功", "致富", "市场",
                 "营销", "销售", "客户", "团队", "领导", "职业", "创业",
                 "创新", "竞争", "品牌", "渠道", "谈判", "演讲", "口才",
                 "影响力", "说服力", "情商", "自控", "自律", "专注",
                 "拖延", "人生", "幸福", "心灵", "智慧", "秘密", "力量",
                 "之道", "之路", "之谜", "法则", "密码", "格局", "境界",
                 "修养", "处世", "礼仪", "人脉", "社交", "情绪", "心态"],
    },
    "实用生活": {
        "hard": [
            "菜谱", "食谱", "家常菜", "烹饪", "烘焙", "甜品", "茶饮",
            "钓鱼", "养花", "盆栽", "宠物", "养狗", "养猫", "装修",
            "家居", "收纳", "编织", "缝纫", "驾驶", "车主",
            "保养手册", "汽车维修", "百科全书", "十万个为什么", "科普",
            "股票", "基金", "期货", "外汇", "投资", "理财", "保险",
            "贷款", "信用", "金融", "货币", "资本", "银行",
            "股市", "选股", "炒股", "股民", "操盘", "K线", "均线",
            "兵器", "枪械", "坦克", "战机", "军舰", "旅游攻略",
            "地图集", "户型", "风水宝典", "起名大全", "孕产", "亲子",
            "家常", "汤谱", "粥谱", "面食", "火锅", "烧烤",
        ],
        "soft": ["家庭", "生活", "旅游", "美食", "手工"],
    },
    "绘本儿童": {
        "hard": [
            "绘本", "图画书", "睡前故事", "胎教故事", "连环画", "小熊宝宝",
            "卡梅拉", "鼠小弟", "米莉茉莉", "幸福的种子", "聪明豆",
            "德德家家", "幼儿", "亲子阅读", "识字", "描红",
            "童话故事大王", "宝宝", "亲子", "早教",
        ],
        "soft": ["童话", "儿歌", "寓言", "睡前"],
    },
    "漫画": {
        "hard": [
            "海贼王", "火影忍者", "名侦探柯南", "死神bleach", "灌篮高手",
            "七龙珠", "银魂", "进击的巨人", "攻壳机动队", "哆啦A梦",
            "蜡笔小新", "樱桃小丸子", "犬夜叉", "钢之炼金术师", "全职猎人",
            "幽游白书", "圣斗士", "城市猎人", "乱马", "浪客剑心", "通灵王",
            "死亡笔记", "黑执事", "妖精的尾巴", "我的英雄学院", "一拳超人",
            "东京喰种", "阿拉蕾", "机器猫", "圣斗士星矢", "棋魂", "网球王子",
            "足球小将", "棒球英豪", "金田一少年", "少年侦探", "柯南",
        ],
        "soft": ["漫画", "单行本", "连载"],
    },
}

# ---------------- 「书名噪声」词：净化名字用，不参与判死
NOISE_WORDS = [
    "扫描版", "文字版", "精校版", "精排版", "精校", "校对版", "典藏版",
    "珍藏版", "精华版", "全彩版", "插图版", "足本", "全本", "完本",
    "完结篇", "完结", "已完结", "完整版", "全集", "全文", "全册", "全三册",
    "套装版", "套装", "修订版", "最新版", "最新", "高清", "高清扫描",
    "图片版", "OCR", "电子书", "电子图书", "kindle", "Kindle", "KINDLE",
    "nodrm", "NODRM", "无广告", "免费下载", "手机电子书", "网络版",
    "更多电子书籍", "更多好书", "精品书籍", "精品", "精品电子书",
    "中亚自购", "自购自破", "中亚", "自破", "自购", "正版", "正版电子书",
    "下载", "txt之家", "小说下载", "校对", "排版", "更多",
    "书籍", "扫描", "珍藏", "典藏", "藏书", "书系", "复件", "副本",
    "寸版", "六寸版", "精品合集", "合集",
    # 站点/来源标记：常粘在文件名尾巴上（`神座_皇甫奇_TXT小说天堂`）。
    # 不去掉的话，它们字多，会被「短名取长名」当成书名捞起来。
    "TXT小说天堂", "txt小说天堂", "小说天堂", "派派小说", "派派txt",
    "醉逍遥书屋", "逍遥书屋", "52ebook", "9iebook", " ebook", "ebook",
    "飞库网", "起点中文网", "纵横中文网", "17k", "逐浪网", "小说阅读网",
    "新浪读书", "腾讯读书", "书香门第", "我爱电子书", "久久小说",
    "言情小说吧", "小说吧", "贴吧",
]

# 整段就是这些的，直接当垃圾删掉——留一本也没意义，还会在去重时
# 把成百上千个无关文件撞成一个名字。
GARBAGE_NAMES = {
    "简介", "内容提要", "内容简介", "目录", "总目录", "合集目录", "书目",
    "说明", "下载说明", "使用说明", "电子书说明", "新建", "新建文本文档",
    "未命名", "无标题", "封面", "扉页", "版权页", "前言", "序言", "序",
    "后记", "附录", "索引", "致谢", "读后感", "书评", "推荐语", "导读",
    "版权", "声明", "免责声明", "readme", "README", "index", "首页",
}

PUB_TAIL = re.compile(
    r"[\s\-_·]*[^\s\-_·]{0,12}"
    r"(?:出版社|出版集团|图书公司|文化公司|出品|发行|编译|译著|主编|编著|"
    r"责任编辑|整理|校注|注释|绘图|插图)")

# 常见姓氏（只留百家姓前 100，太偏的姓容易把书名误判成作者）
SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾萧田董袁潘"
    "蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金廖贾夏韦付方白邹孟熊秦邱江尹薛"
    "闫段雷侯龙史贺倪汤滕殷罗毕郝安常傅齐康伍余元顾毛狄苗戚纪梅盛林刁钟")
COMPOUND = ("欧阳", "上官", "司马", "诸葛", "东方", "独孤", "南宫", "夏侯",
            "皇甫", "尉迟", "公孙", "慕容", "长孙", "宇文", "司徒", "轩辕")

# 只按「强分隔符」切段：中文标点（，、。）算书名内部字符，不切
SEG_SPLIT = re.compile(r"[\s\-—–_·・:：|｜/\\]+")
BRACKET = re.compile(r"[（(【\[][^（)（】\])]{0,30}[)）】\]]")
BAD_CHARS = re.compile(r"[\[\]【】()（）<>《》\"'“”‘’]")

# ------------------------------------------------------------ 语言/工具
CJK_CH = r"一-鿿"


def strip_ext(name, ext):
    low = name.lower()
    if ext and low.endswith("." + ext.lower()):
        return name[: -(len(ext) + 1)]
    return os.path.splitext(name)[0]


def _unescape(s):
    return html.unescape(html.unescape(s))


def base_stem(filename, ext):
    return _unescape(strip_ext(filename, ext))


# 去掉空格/数字/标点后还是这些词的，就是书里的前言目录页，不是小说。
# 不先压平的话 `目  录 (24)` 会被空格切成「目」「录24」两段，谁也认不出来。
FLAT_JUNK = re.compile(r"[\s\d\(\)（）\[\]【】·_\-—–.。，、]+")


def classify(filename, ext):
    """返回 (删吗, 原因)"""
    stem = base_stem(filename, ext)
    if FLAT_JUNK.sub("", stem) in GARBAGE_NAMES:
        return True, "无意义名"
    if ext.lower() in ("ppt", "pps"):
        return True, "幻灯片/演示稿"
    if filename.startswith("._") or filename.startswith("~$"):
        return True, "苹果垃圾副本"
    if ext.lower() not in KEEP_FMT:
        return True, "非书格式"
    lang = lang_of(stem)
    # 繁体**不删**——留着，改名那一步转成简体（见 to_simplified）
    if lang != "none" and lang != "繁体":
        return True, "非中文(" + lang + ")"
    fam_hit = []
    for fam, cfg in FAMILIES.items():
        if any(kw in stem for kw in cfg["hard"]):
            return True, fam
        for rx in cfg.get("re", []):
            if re.search(rx, stem):
                return True, fam
        if any(kw in stem for kw in cfg["soft"]):
            fam_hit.append(fam)
    # 「X学」结尾基本都是学科专著（排除 我的大学 / 小学 这类）
    core = pick_title(find_stem_minus_vol(filename, ext)) or stem
    flat = re.sub(r"[\s\-_·.。，、（）()\[\]【】《》]", "", core)
    if re.search(r"[" + CJK_CH + r"]{2,}学$", flat) and \
            not any(x in flat for x in XUE_EXCEPT):
        return True, "学术专著(X学)"
    # 书名太长的（≥12 字）多半是带副标题的非虚构，算一条「软嫌疑」
    if len(core) >= 12:
        fam_hit.append("长标题")
    if len(fam_hit) >= 2:
        return True, "多类嫌疑(" + "+".join(fam_hit[:3]) + ")"
    return False, ""


# ------------------------------------------------------------------ 改名
def _drop_brackets(s):
    prev = None
    while prev != s:
        prev = s
        # 括号里是纯数字的先「去括号留数字」：那多半是册号/章节号
        # （世界科技全景百卷书(91)环境科学）、而不是副本序号。
        # 老逻辑连数字一起删，于是一套书几十册全撞成同一个名字被去重误删。
        # 同名副本的编号是相同的（都是 (1)），留着也照样能去重。
        s = re.sub(r"[（(【\[]\s*(\d{1,4})\s*[)）\]】]", r"\1", s)
        s = BRACKET.sub(" ", s)
    return s


# `[日]` `[美]` 这类国别标签——必须带括号才算，光秃秃的「法」「德」是书名常用字
COUNTRY = re.compile(
    r"^[\[\(（【]\s*(日|美|英|法|德|俄|苏|意|西|荷|韩|加|澳|印|日本|美国|"
    r"英国|法国|德国|俄国|苏联|意大利|西班牙|荷兰|瑞典|挪威|丹麦|芬兰|波兰|"
    r"捷克|加拿大|澳大利亚|印度|巴西|阿根廷)\s*[\]\)）】]?")


# 从整库里「反查」出来的常用作者名（出现次数够多、且基本只当副标题出现）
AUTHOR_VOCAB = set()
PREFIX_VOCAB = set()      # 常驻前缀（作者/系列名），见 set_author_vocab
NAME_VOCAB = set()        # 不分位置的「人名表」，见 set_author_vocab

# NAME_VOCAB 的排除表：这些词和作者一样「几乎从不单独出现」，但它们不是人名。
# 不放进来的话，`莲花-安妮宝贝` 里的书名会被当成作者换掉、`柳残阳 沥血伏龙`
# 之类也会挑错段。
NAME_STOP = {
    # 卷册/位置词
    "上册", "上部", "下册", "下部", "中部", "中学卷", "全本", "完整版",
    "单行本", "自选集", "之一", "第三部分", "第二部分", "第一大招",
    "第三大招",
    # 角色/说明词
    "作者", "主编", "编著", "译者", "译注", "佚名", "不详", "不题撰人",
    "文本文档", "文档", "新建", "替换", "超高收藏", "高收藏", "七天一书",
    # 书架分类词
    "人文类", "儿童类", "文学类", "经管类", "英文类", "评书类", "相声类",
    "小说类", "武侠古典", "武侠版", "武侠", "言情", "科幻", "穿越", "网游",
    "都市", "种田", "种田文", "恐怖", "现代文学", "世界文学", "古代文学",
    "女性文学", "历史传记", "历史探索", "军事历史", "地理科普", "传统国学",
    "宗教文化", "亲子早教", "健康养生", "人力资源", "商场职场", "投资理财",
    "经济金融", "经管案例", "经营管理", "教育辅助", "精典教育", "知识启蒙",
    "百科文化", "社会百态", "异域珍闻", "综合报道", "讲座访谈", "论坛",
    "新闻", "知乎", "知乎周刊", "知日", "通信", "音乐", "影视录音", "广播剧",
    "话剧", "台湾相声", "相声大全", "相声名家", "德云社", "评书大全",
    "儿歌大全", "小品大全", "育儿宝典", "能力培养", "年轻退休", "生命领悟",
    "沟通礼仪", "职场修炼", "品牌英语", "英语教育", "英语名著", "新东方",
    "华图教育", "图说天下", "时代书城", "新知文库", "学友文库", "雏鹰文库",
    "數學", "果壳", "童话故事", "散文系列",
    # 朝代 / 史书（`书名-朝代-作者` 里的中间段，不是人名）
    "东晋", "西晋", "五代", "后汉", "后秦", "后魏", "南朝宋", "前蜀",
    "元魏", "姚秦", "刘宋", "中古时代", "北史", "魏书", "隋书", "金史",
    "梁书", "南齐书", "旧五代史", "证治准绳", "续道藏",
    # 站点
    "派派小说", "西陆社区", "再痕中文",
}


def set_author_vocab(stems):
    """把「总出现在分隔符之后、2~4 字、出现 ≥3 次」的段当成作者名记下来

    这样即使遇到 `迷航昆仑墟』天下霸唱著` 这种没有分隔符的文件名，也能把
    尾巴上的作者名切掉。
    """
    global AUTHOR_VOCAB, PREFIX_VOCAB, NAME_VOCAB
    cnt, head, alone = (collections.Counter(), collections.Counter(),
                        collections.Counter())
    for s in stems:
        segs = [x for x in SEG_SPLIT.split(s) if x and CJK.search(x)]
        for i, x in enumerate(segs):
            cnt[x] += 1
            if i == 0:
                head[x] += 1
        if len(segs) == 1:
            alone[segs[0]] += 1
    AUTHOR_VOCAB = {
        x for x, c in cnt.items()
        if c >= 3 and head[x] / c <= 0.2
        and re.fullmatch(r"[" + CJK_CH + r"]{2,4}", x)}
    # 不分位置的「人名表」：作者名有个很硬的特征 —— 它几乎从不会单独成一个
    # 文件名（`柳残阳.txt` 没人这么存），而真正的书名会（`盗墓笔记.epub`）。
    # 于是「出现 ≥20 次 + 单独出现率 ≤3%」就能把作者/系列名捞出来，
    # 不用管它在前还是在后。柳残阳（首段率 0.90）、温瑞安（0.59）、
    # 卧龙生（0.49）这些「前后都出现」的作者，靠 head 比例哪张表都进不去。
    NAME_VOCAB = {
        x for x, c in cnt.items()
        if c >= 20 and alone[x] / c <= 0.03
        and re.fullmatch(r"[" + CJK_CH + r"]{2,4}", x)
        and x not in NAME_STOP and not _looks_cat(x)
        and not re.fullmatch(r"(上|中|下)(册|部|卷|篇|集)", x)}
    # 常驻前缀：几乎总在第一段，而且几乎从不单独成段 —— 也就是它后面总跟着
    # 别的东西（`柳残阳 沥血伏龙`、`脱脱 金史 本纪第006`）。这种是作者/系列名，
    # 不是书名；认出来才不会把同一作者的几十部作品撞成一个名字。
    # 「几乎不单独成段」这个条件很关键：畅销书（盗墓笔记.epub）单段出现几十次，
    # 加上它才不会被误判成前缀、把书名换成副标题。
    # 门槛要够狠：放行过宽会把《三国演义》《乌合之众》这类畅销书名也收进来，
    # 于是「三国演义 毛宗岗点评」被换成「毛宗岗点评」，书名反而没了。
    PREFIX_VOCAB = {
        x for x, c in cnt.items()
        if c >= 12 and head[x] / c >= 0.9 and alone[x] / c <= 0.1
        and re.fullmatch(r"[" + CJK_CH + r"]{2,4}", x)}


def strip_trailing_author(t):
    """书名尾巴上粘着的作者名（没有分隔符那种）"""
    cjk = len(CJK.findall(t))
    for n in (4, 3, 2):
        # 剩下的必须还够一个书名（≥4 字）。原阈值 3 会把 `郝恬谧的生活`
        # 切成 `郝恬谧的` ——「生活」在整库里出现得多，被当成了作者名。
        if cjk - n < 4 or t[-n:] not in AUTHOR_VOCAB:
            continue
        # 光「出现过几次」不够：`不要`、`生活` 这种常用词在整库里也会反复出现
        # 在分隔符之后，于是 `迟来的白马要不要` 被切成 `迟来的白马要`、
        # `郝恬谧的生活` 被切成 `郝恬谧的`。必须再过一道姓氏/人名表才算作者。
        if not (t[-n:] in NAME_VOCAB or t[-n:][0] in SURNAMES
                or t[-n:][:2] in COMPOUND):
            continue
        left = t[:-n]
        # 切完剩半句话（...的 / ...之 / ...与）说明切错了，别切
        if left[-1:] in "的之与和跟了对在是为":
            continue
        return left
    return t


# 分类标签：书库按类分卷时留下的前缀，不是书名的一部分。
# 典型 `B 小说类 恐怖 (10).txt` → 老逻辑取第一段得「小说类」，
# 于是同一分类下成百上千个文件全部撞名、去重后只剩一个。
CAT_RE = re.compile(r"^[A-Za-z]?\s*[" + CJK_CH + r"]{2,4}类$")
CAT_WORDS = {"武侠古典", "古典武侠", "现代文学", "当代文学", "网络文学",
             "历史探索", "经营管理", "英语名著", "评书大全", "小说大全",
             "精品小说", "言情小说", "武侠小说", "科幻小说", "恐怖小说",
             "侦探小说", "古典文学", "外国文学", "中国文学", "纪实文学"}


# 「前缀-书名」结构：`卫斯理--地底奇人`、`寄秋-茶意绵绵`
# 放宽到 6 字：`汤姆克朗西--猎杀红色十月` 里的译名作者有 5 个字，
# 卡在 4 字上限上就认不出「作者--作品」结构，书名会取成作者名。
DASH_HEAD = re.compile(r"^\s*[" + CJK_CH + r"]{1,6}[-—–·]{1,2}")
# 「书名_作者_站点」结构：`永生_梦入神机_TXT小说天堂` —— 第一段才是书名
UNDER_HEAD = re.compile(r"^\s*[" + CJK_CH + r"]{1,4}_")


# 章回号：`第一章 迎来送往` 里的「第一章」是编号不是书名，丢掉才能取到后面的
# 章节标题。整段只剩章回号的（`第0030篇`）在「垃圾/无效名」里已经判死了。
# 「零/百/千」必须在这里：`第一百零一节` 少了「零」就匹配不上
CN_NUM_CH = r"0-9零〇一二三四五六七八九十百千两"

CHAP_RE = re.compile(
    r"^第\s*[" + CN_NUM_CH + r"]{1,6}\s*"
    r"(?:章|节|節|回|讲|講|篇|集|卷|部|册|部分|话|話)\s*$")


def _looks_cat(seg):
    """像分类标签/章回号就丢掉（`小说类` / `武侠古典` / `第一章`）"""
    s = seg.strip()
    return bool(CAT_RE.match(s)) or s in CAT_WORDS or bool(CHAP_RE.match(s))


def _looks_author(seg):
    """像作者名就丢掉

    三种样子：带国别标签（`[日]太宰治`）、`D.莫里斯` 这种带西文缩写的人名、
    2~3 字常见姓开头（`苏力`）或 4 字复姓（`欧阳XX`）；另外整库反查出来的
    高频作者名（见 set_author_vocab）也算。
    """
    if COUNTRY.match(seg):
        return True
    core = re.sub(r"^[^" + CJK_CH + r"]+", "", seg)
    if core != seg and re.fullmatch(r"[" + CJK_CH + r"]{2,4}", core):
        return core[0] in SURNAMES or core[:2] in COMPOUND \
            or core in AUTHOR_VOCAB
    if not re.fullmatch(r"[" + CJK_CH + r"]{2,4}", seg):
        return False
    if seg[:2] in COMPOUND and len(seg) == 4:
        return True
    if seg in AUTHOR_VOCAB or seg in PREFIX_VOCAB or seg in NAME_VOCAB:
        return True
    return len(seg) <= 3 and seg[0] in SURNAMES


def pick_title(stem):
    s = stem.strip()
    m = re.fullmatch(r"《(.+?)》", s)
    if m:
        s = m.group(1).strip()
    s = s.replace("《", " ").replace("》", " ")
    s = _drop_brackets(s)
    s = re.sub(r"(?<=[" + CJK_CH + r"])[Vv](?=[" + CJK_CH + r"])", " ", s)
    s = s.replace("+", " ").replace("＋", " ")
    for w in NOISE_WORDS:
        s = s.replace(w, " ")
    s = PUB_TAIL.sub(" ", s)
    s = re.sub(r"[\s\-_·]*(?:作者|著者|译者)?\s*[:：]?\s*[著译编作]\s*$", " ", s)
    segs = [x for x in SEG_SPLIT.split(s) if x]
    # 纯数字的段是编号不是噪声，并回前一段（`上市公司_13`、`卷117`）。
    # 直接丢掉的话，不同编号的文件会撞成同一个名字，被去重大批误删。
    merged = []
    for x in segs:
        if re.fullmatch(r"\d{1,4}", x) and merged and CJK.search(merged[-1]):
            merged[-1] += x
        else:
            merged.append(x)
    segs = [x for x in merged if CJK.search(x)]
    if not segs:
        return ""
    segs = [x for x in segs if not _looks_cat(x)] or segs   # 先甩掉分类标签
    rest = [x for x in segs if not _looks_author(x)] or segs
    pick = rest[0]
    pick = BAD_CHARS.sub("", pick)
    pick = COUNTRY.sub("", pick)                           # 去掉 [日]/（美）这类标签
    # 第一段短得不像书名（≤3 字），而后面有一段明显更像正文 → 认长的那个。
    # 典型：`寄秋-茶意绵绵` 里的「寄秋」是作者，真正的书名是后面的「茶意绵绵」。
    # 不这么做，同一作者的几十部作品会全部撞成「寄秋」一名，去重时只留一本，
    # 其余全被误删（实测这一条就误伤了上万条）。
    n0 = len(CJK.findall(pick))
    if len(rest) > 1:
        nxt = COUNTRY.sub("", BAD_CHARS.sub("", rest[1]))
        nn = len(CJK.findall(nxt))
        if DASH_HEAD.match(s) and "_" not in s:
            # `作者-作品` / `系列--分册`（卫斯理--地底奇人、寄秋-茶意绵绵、
            # 汤姆克朗西--猎杀红色十月）：后面那段就是书名，直接取它。
            #
            # 三条例外：
            # · 带下划线的多半是 `书名-作者_站点`（枭霸-柳残阳_TXT小说天堂），
            #   第一段才是书名，不能换；
            # · 后一段不足 2 字则不换：`全唐文-清-董诰`、`水浒传-明-施耐庵`
            #   里的朝代名是单字，换过去会让几十种古籍全撞成「清」「明」；
            # · 后一段本身就是作者名不换：`莲花-安妮宝贝`、`春宴-安妮宝贝`
            #   换成「安妮宝贝」等于把书名丢掉、把作者扶正。
            #
            # 「后一段更长 或 前段短得不像书名」才换。只按长度比会在等长时
            # 换不动（卫斯理--蓝血人 两段都是 3 字）；不比长度又会把
            # `雪中悍刀行-烽火戏诸侯` 换成作者名。
            if (nn >= 2 and nxt not in NAME_VOCAB and not _looks_author(nxt)
                    and (nn > n0 or n0 <= 3)):
                pick = nxt
        elif n0 <= 3 and not UNDER_HEAD.match(s):
            # 下划线分隔的多半是 `书名_作者_站点`（永生_梦入神机_TXT小说天堂），
            # 第一段才是书名，这里不能动。其余情况（`书名 副标题`）要长一截才换，
            # 免得把「红楼梦 脂砚斋评」换成副标题。
            cands = [COUNTRY.sub("", BAD_CHARS.sub("", x))
                     for x in rest if x not in NAME_VOCAB] or rest
            longer = max(cands, key=lambda x: len(CJK.findall(x)))
            if len(CJK.findall(longer)) >= n0 + 2:
                pick = longer
    if len(CJK.findall(pick)) > 3 and pick[-1:] in ("著", "译", "作", "编"):
        pick = pick[:-1]
    if pick.endswith("的") and len(rest) > 1:             # 书名被切成了两半
        pick = "".join(rest)
    else:
        pick = strip_trailing_author(pick)
    # 结尾的副本序号：只吃「前面是分隔符」的 1~2 位数字（`书名 2` / `书名_2`）。
    # 老写法 re.sub(r"\d{1,2}$", "") 会误伤紧跟汉字的数字：
    #   百度知道072 → 百度知道0、卷117 → 卷1、列传第083 → 列传第0
    # 于是几十个不同编号的文件撞成同一个名字，被去重大批误删。
    # 只吃「空格 + 1~2 位数字」这种副本序号。下划线/点后的数字多半是编号
    # 而不是副本（上市公司_13、阿信(27)），一口吃掉会把不同编号撞成同一个名字。
    pick = re.sub(r"\s\d{1,2}$", "", pick)
    m = re.match(r"^(\d{2,}|[A-Za-z]{1,2}\d{2,4})(["
                  + CJK_CH + r"].*)$", pick)
    # 但 `30天学会…`、`60个瞬间` 里的数字是书名的一部分，不能切
    if m and len(CJK.findall(m.group(2))) >= 4 and m.group(2)[0] not in MEASURE:
        pick = m.group(2)
    # 全角连字符（－—–）也得一起剥掉，否则 `飞狐的故事－孔庆东` 会留下个尾巴
    return pick.strip(" \t-_·.。，、－—–~～")


RE_VOL_CN = re.compile(r"第\s*([" + CN_NUM_CH + r"]{1,6})\s*"
                       r"(?:卷|部|季|册|辑|集)")
RE_VOL_AFTER = re.compile(r"(?:卷|Vol\.?|vol|VOL)\s*"
                          r"([" + CN_NUM_CH + r"]{1,6})(?=\W|$)")
RE_PART = re.compile(r"(上|下)(?:部|卷|册|篇|集)?")
RE_BARE_PART = re.compile(r"^(.*?[" + CJK_CH + r"])\s*([上下])$")
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


_CN_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def _cn2num(s):
    """`二十三` → 23、`一百零一` → 101、`十` → 10

    老写法只认「XY十Z」这一种，遇到 `第一百零一卷` 就返回 None，
    那一册就拿不到卷号后缀，会和同名的其他册撞在一起。
    """
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_DIG and c not in _CN_UNIT for c in s):
        return None
    total = section = number = 0
    for c in s:
        if c in _CN_DIG:
            number = _CN_DIG[c]
        else:
            section += (number or 1) * _CN_UNIT[c]
            number = 0
    return total + section + number


_CN_VOL = {v: k for k, v in _CN_NUM.items() if v and k != "两"}


def _num_cn(n):
    if n >= 100:
        return str(n)          # 上百的卷号直接用阿拉伯数字，别拼出「十十一」
    if n <= 10:
        return _CN_VOL.get(n, str(n))
    if n < 20:
        return "十" + _CN_VOL.get(n - 10, "")
    t, o = divmod(n, 10)
    return _CN_VOL.get(t, "") + "十" + (_CN_VOL.get(o, "") if o else "")


def find_volume(stem, allow_bare=False):
    m = RE_VOL_CN.search(stem)
    if m:
        n = _cn2num(m.group(1))
        if n and 1 <= n <= 99:
            return "vol", n, m.group(0)
    m = RE_VOL_AFTER.search(stem)
    if m:
        n = _cn2num(m.group(1))
        if n and 1 <= n <= 99:
            return "vol", n, m.group(0)
    for m in RE_PART.finditer(stem):
        a, b = m.start(), m.end()
        before = stem[a - 1] if a > 0 else " "
        after = stem[b] if b < len(stem) else " "
        if not CJK.match(before) and not CJK.match(after):
            return "part", m.group(1), m.group(0)
    if allow_bare:
        m = RE_BARE_PART.match(stem.strip())
        if m and len(m.group(1)) >= 3:
            return "part", m.group(2), m.group(2)
    return None, None, None


def vol_suffix(kind, val):
    if kind == "part":
        return "-" + ("上部" if val == "上" else "下部")
    if kind == "vol":
        return "-第" + _num_cn(val) + "卷"
    return ""


def clean_name(filename, ext, allow_bare=False):
    stem = base_stem(filename, ext)
    kind, val, raw = find_volume(stem, allow_bare=allow_bare)
    suffix = vol_suffix(kind, val) if kind else ""
    if raw:
        stem = stem.replace(raw, " ")
    title = pick_title(stem)
    if not title:
        return filename, suffix, False
    title = re.sub(r"[?*<>|\r\n\t]", "", title).strip()
    if not title:
        return filename, suffix, False
    # 省略号在文件名里写成一串点（`爱，那么痛......之微寒`），留着会让
    # 扩展名判断出问题，直接去掉
    title = re.sub(r"\.{2,}", "", title)
    # 整段没切开时章回号还挂在前面；可能套了几层
    # （`第六章第一百零一节笔仙之祸` 两层都要剥）
    stripped = title
    for _ in range(3):
        nxt = re.sub(r"^第\s*[" + CN_NUM_CH + r"]{1,6}\s*"
                     r"[章节節回讲講篇话話]\s*", "", stripped)
        if nxt == stripped or len(CJK.findall(nxt)) < 2:
            break
        stripped = nxt
    if len(CJK.findall(stripped)) >= 2:
        title = stripped
    if not title.strip():
        return filename, suffix, False
    title = to_simplified(title)            # 繁体书名统一转简体
    new = title + suffix + "." + (ext or "")
    return new, suffix, new != filename


# ------------------------------------------------------------------ 主流程
def load_rows():
    with open(CSV_IN, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r), list(r.fieldnames)


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "summary"
    rows, fields = load_rows()
    set_author_vocab([base_stem(r["文件名"], r["后缀"]) for r in rows])
    del_cnt = collections.Counter()
    del_samples = collections.defaultdict(list)
    keeps = []
    for row in rows:
        name = row["文件名"]
        ext = (row.get("后缀") or "").strip()
        bad, why = classify(name, ext)
        if bad:
            del_cnt[why] += 1
            if len(del_samples[why]) < 60:
                del_samples[why].append(name)
            row["_op"] = "delete"
        else:
            row["_op"] = "keep"
            keeps.append(row)

    # 第一遍：只认明确的卷号，先拿一个「候选书名」
    for row in keeps:
        ext = (row.get("后缀") or "").strip()
        _, _, _ = clean_name(row["文件名"], ext, allow_bare=False)
        row["_t0"] = pick_title(find_stem_minus_vol(row["文件名"], ext))
    cnt0 = collections.Counter(
        re.sub(r"[上下]$", "", r["_t0"]) for r in keeps)

    vols = collections.Counter()
    for row in keeps:
        ext = (row.get("后缀") or "").strip()
        bare = cnt0.get(re.sub(r"[上下]$", "", row["_t0"]), 0) >= 2
        new, suffix, changed = clean_name(row["文件名"], ext, allow_bare=bare)
        if suffix:
            vols[suffix] += 1
        row["_new"] = new
        row["_changed"] = changed
        row["_base"] = os.path.splitext(new)[0]
        rank = FMT_RANK.get(ext.lower(), 99)
        row["_rank"] = (rank, -int(row.get("大小(字节)") or 0))

    # 改名后成了「简介 / 目录 / 新建 文本文档」这类通用名的，整组删掉：
    # 留一本也没意义，而且它们会把成千上万个无关文件撞成一个名字，
    # 让去重统计虚高（实测仅「简介」「目录」两组就吃掉上万条）。
    for row in keeps:
        if row["_op"] != "keep":
            continue
        base = os.path.splitext(row["_new"])[0].strip()
        # 尾部那串数字多半是副本序号，剥掉再比，否则 `简介3`、`目录24` 会漏网
        if (base in GARBAGE_NAMES
                or re.sub(r"\d+$", "", base) in GARBAGE_NAMES
                # 只剩一个汉字的（`脊` / `酒` / `第251`）是切碎的残渣，
                # 认不出是什么书，留着还会和别的碎片撞名
                or len(CJK.findall(base)) <= 1):
            row["_op"] = "delete"
            del_cnt["无意义名"] += 1
            if len(del_samples["无意义名"]) < 60:
                del_samples["无意义名"].append(row["文件名"])

    by_base = collections.defaultdict(list)
    for row in keeps:
        if row["_op"] == "keep":
            by_base[row["_base"]].append(row)
    dup_groups = []
    for base, group in by_base.items():
        if len(group) == 1:
            continue
        group.sort(key=lambda r: r["_rank"])
        dup_groups.append((base, [g["文件名"] for g in group]))
        for extra in group[1:]:
            extra["_op"] = "delete"
    del_cnt["同名重复"] = sum(len(g) - 1 for _, g in dup_groups)
    dup_groups.sort(key=lambda x: -len(x[1]))
    final_keep = [r for r in keeps if r["_op"] == "keep"]

    lines = []
    a = lines.append
    a(f"原始条目：{len(rows)}")
    a(f"删除合计：{sum(del_cnt.values())}")
    a(f"最终保留：{len(final_keep)}（改名 {sum(1 for r in final_keep if r['_changed'])} 条）")
    a("")
    a("=== 删除原因分布 ===")
    for k, v in del_cnt.most_common():
        a(f"{k}\t{v}")
    a("")
    a("=== 卷号后缀分布 ===")
    for k, v in vols.most_common(40):
        a(f"{k}\t{v}")
    random.seed(11)
    a("")
    a("=== 保留样本（随机 500）===")
    for r in random.sample(final_keep, min(500, len(final_keep))):
        a(f"{r['文件名']}\t->\t{r['_new']}")
    a("")
    a("=== 各删除原因样本 ===")
    for k in sorted(del_samples):
        a(f"--- {k}")
        for n in del_samples[k]:
            a(f"    {n}")
    a("")
    a("=== 同名重复最多的 200 组 ===")
    for base, grp in dup_groups[:200]:
        a(f"{base}\t{grp}")
    txt = "\n".join(lines)
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt[:3600])

    if stage == "apply":
        if not os.path.exists(CSV_IN + ".bak"):
            shutil.copy2(CSV_IN, CSV_IN + ".bak")
            print("原表已备份：", CSV_IN + ".bak")
        with open(CSV_IN, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in rows:
                if row["_op"] == "delete":
                    row["操作"] = "删除"
                    row["新文件名"] = ""
                elif row["_changed"]:
                    row["操作"] = "改名"
                    row["新文件名"] = row["_new"]
                else:
                    row["操作"] = ""
                    row["新文件名"] = ""
                w.writerow({k: row.get(k, "") for k in fields})
        print("已写回：", CSV_IN)


def find_stem_minus_vol(filename, ext):
    stem = base_stem(filename, ext)
    _, _, raw = find_volume(stem, allow_bare=False)
    return stem.replace(raw, " ") if raw else stem


if __name__ == "__main__":
    main()
