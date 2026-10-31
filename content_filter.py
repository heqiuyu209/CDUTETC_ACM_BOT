"""回答内容过滤模块（五层防护中的本地判定层，纯函数可离线测试）。

本模块只做本地、确定性判断，不调用任何模型、不访问网络：
  1) 话题范围硬限制（is_topic_relevant）：问题与 ACM 集训队/比赛/训练/队务
     明显无关时直接拒绝，不调模型；llm.py 的 system prompt 软约束保留不变。
  2) 敏感内容拦截（has_sensitive）：政治 / 色情 / 暴力 / 广告 / 诈骗 / 涉密
     等关键词与正则匹配，命中即拒绝。
  3) 输出过滤（sanitize_output）：回复文本二次敏感检查 + 长度上限截断 +
     禁止超长链接（超长 URL 替换为占位符）。
  4) 频率防刷（RateLimiter）：按用户（key）滑动窗口限流，供 main.py 使用。

词表设计原则：内置词表以“中性、可拦截、少误伤”为准，部署方可随时通过
环境变量 ACM_QA_SENSITIVE_EXTRA_WORDS（逗号分隔）追加自定义敏感词，
无需修改本文件。
"""

import re
import threading
import time

import config


def _bigrams(text: str) -> set[str]:
    """取中文/字母数字字符的二元组，作为轻量相似度特征（无需分词库）。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def text_similarity(a: str, b: str) -> float:
    """二元组 Dice 系数，取值 0~1；用于本地知识库检索打分。"""
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return 2.0 * len(sa & sb) / (len(sa) + len(sb))


# 标题切词时丢弃的虚词，避免“本周/什么”这类词造成误命中
_TITLE_STOP_TOKENS = {
    "什么", "怎么", "请问", "这个", "那个", "我们", "你们", "可以", "时候",
    "哪些", "哪个", "多少", "如何", "为什", "是不", "不是", "没有", "有没",
}


def _title_tokens(title: str) -> set[str]:
    """把标题切成可用于关键词命中的词元。

    规则（不依赖分词库）：
    - 英文/数字连续串作为整体词元（长度 ≥2）；
    - 中文连续串作为整体词元（长度 ≥2），长度 >3 时再补长度 3 的滑窗子串。
    这样「ACM 周赛（本周）」→ {acm, 周赛, 本周}，
    「成都理工大学ACM排名赛」→ {成都理工大学, 成都理, 都理工, ...}，
    既能命中「周赛」这种短词，又不会因为「大学」这种泛词误命中。
    """
    tokens: set[str] = set()
    for chunk in re.findall(r"[A-Za-z]+|\d+|[\u4e00-\u9fff]+", title or ""):
        lowered = chunk.lower()
        if chunk.isascii():
            if len(chunk) >= 2:
                tokens.add(lowered)
            continue
        if len(chunk) >= 2:
            tokens.add(lowered)
        if len(chunk) > 3:
            for i in range(len(chunk) - 2):
                tokens.add(chunk[i:i + 3])
    return {t for t in tokens if t and t not in _TITLE_STOP_TOKENS}


def title_keyword_score(question: str, title: str) -> float:
    """标题关键词命中评分：标题词元出现在问题里即给分，0~0.9。

    用于补 Dice 系数对口语问法的不足。例如「这周有周赛吗」与
    「ACM 周赛（本周）」的 Dice 只有 0.18，但「周赛」这个标题词元命中，
    应判为同一条记录。词元越长越可信：2 字 0.6、3 字 0.7、4 字 0.8。
    """
    q = (question or "").lower()
    if not q:
        return 0.0
    best = 0.0
    for token in _title_tokens(title):
        if token in q:
            best = max(best, min(0.4 + 0.1 * len(token), 0.9))
    return best


# 口语同义归一：把常见说法映射到记录里更可能出现的写法
_QUESTION_SYNONYMS = (
    ("这个星期", "本周"), ("这个礼拜", "本周"),
    ("这星期", "本周"), ("这礼拜", "本周"), ("这周", "本周"),
    ("今晚", "今天晚上"), ("明晚", "明天晚上"),
)


def _normalize_question(text: str) -> str:
    q = text or ""
    for src, dst in _QUESTION_SYNONYMS:
        q = q.replace(src, dst)
    return q


def record_match_score(question: str, *fields: str) -> float:
    """综合 Dice 相似度与标题关键词命中，取较高者作为记录匹配分。"""
    normalized = _normalize_question(question)
    best = 0.0
    for field in fields:
        if not field:
            continue
        best = max(
            best,
            text_similarity(normalized, field),
            title_keyword_score(normalized, field),
            title_keyword_score(question, field),
        )
    return best


# 问句里的语气/疑问/时间噪声词。按长度从大到小排列，先删长词避免残留。
_QUESTION_NOISE = (
    "这礼拜", "这星期", "这个星期", "这个礼拜", "有没有", "是不是", "能不能",
    "这周", "本周", "那个", "这个", "请问", "问一下", "有没", "什么", "怎么",
    "哪些", "哪个", "多少", "如何", "可以", "一下",
    "吗", "呢", "吧", "啊", "呀", "哦", "嘛", "没", "了", "的", "有", "是",
    "在", "会", "要", "还", "都", "和", "与",
)


def question_core(text: str) -> str:
    """抽取问句核心词：去掉标点、空白与语气词。

    「这周有周赛吗」「这周有周赛没」「周赛有没」都会归一成「周赛」，
    用于判断两个问法是否同一意图——比整句相似度稳定得多。
    """
    q = re.sub(r"[\s\W_]+", "", (text or "").lower())
    for word in _QUESTION_NOISE:
        q = q.replace(word, "")
    return q


def same_intent(a: str, b: str) -> bool:
    """两个问法是否指向同一问题：核心词相同或互相包含。"""
    ca, cb = question_core(a), question_core(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    short, long = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return len(short) >= 2 and short in long


def question_match_score(question: str, stored: str) -> float:
    """问句之间的匹配分：同意图直接给满分，否则退回通用记录匹配分。"""
    if same_intent(question, stored):
        return 1.0
    return record_match_score(question, stored)


# ----------------------------------------------------------------------
# 1) 话题范围硬限制：本地意图判断（不调 LLM）
# ----------------------------------------------------------------------
# 命中任一关键词即视为与 ACM 队务相关。词表按语义分组，便于维护；
# 纯关键词匹配是保守策略：宁可“少拦”（边界文本放行给模型软约束），
# 也不误伤正常队内提问；明显无关的文本（不命中任何词）直接拒绝。
_TOPIC_KEYWORDS = (
    # 队名 / 赛事体系
    "acm", "集训", "集训队", "icpc", "ccpc", "蓝桥杯", "天梯赛", "校赛", "省赛",
    "区域赛", "邀请赛", "周赛", "选拔赛", "新生赛", "挑战赛", "月赛",
    # 比赛 / 训练动作
    "比赛", "竞赛", "训练", "训练赛", "练习", "备赛", "报名", "参赛", "题目",
    "题解", "出题", "签到题", "排名", "rating", "rank", "榜单", "晋级", "名额",
    "奖项", "奖牌", "提交", "通过率", "罚时", "封榜",
    # 技术主题
    "算法", "数据结构", "动态规划", "贪心", "搜索", "图论", "数论", "字符串",
    "线段树", "树状数组", "并查集", "dfs", "bfs", "二分", "模拟", "排序",
    "哈希", "dp", "快排", "递归", "栈", "队列", "链表", "二叉树", "网络流",
    "矩阵", "高精度", "c++", "c语言", "python", "java", "oj", "codeforces",
    "atcoder", "牛客", "牛客网", "leetcode", "洛谷", "vjudge", "力扣",
    # 队内事务 / 环境
    "机房", "答疑", "辅导", "教练", "指导老师", "队伍", "队友", "签到", "考勤",
    "课程", "上课", "作业", "课件", "服务器", "评测机", "工位", "退役",
    # 新生高频事务提问（入学/装备/地点/流程）：这些是机器人最核心的答疑场景，
    # 必须视为相关，否则“带电脑吗/需要支架吗/二机位”会被误判为无关而拒答。
    "电脑", "笔记本", "支架", "二机位", "摄像头", "耳麦", "设备", "手机",
    "课表", "教室", "地点", "集合", "准备", "材料", "费用", "条件", "要求",
    "加入", "招新", "注册", "平台", "账号", "环境", "配置", "软件", "工具",
)
_TOPIC_RE = re.compile("|".join(re.escape(k) for k in _TOPIC_KEYWORDS), re.IGNORECASE)


def is_topic_relevant(text: str) -> bool:
    """本地意图判断：文本命中任一 ACM 相关话题词视为相关，否则视为无关。"""
    if not text:
        return False
    return bool(_TOPIC_RE.search(text))


# ----------------------------------------------------------------------
# 2) 敏感内容拦截
# ----------------------------------------------------------------------
# 内置敏感词表（默认启用，覆盖政治 / 色情 / 暴力 / 广告 / 诈骗 / 涉密）。
# 词汇均为中性表述；需要更贴合本队场景时用 config 扩展词追加。
_SENSITIVE_WORDS = (
    # 政治 / 涉密
    "台独", "疆独", "藏独", "法轮功", "民运", "反华", "颠覆国家", "国家机密",
    "机密文件", "绝密", "内部文件", "内部资料", "红头文件", "涉密", "泄露机密",
    # 色情 / 低俗
    "裸聊", "约炮", "一夜情", "色情", "援交", "嫖娼", "卖淫",
    # 暴力 / 恐吓
    "杀人", "砍人", "炸弹", "枪支", "弹药", "爆炸物", "恐怖袭击", "血腥",
    "报复社会", "持刀",
    # 诈骗 / 黑产
    "刷单", "兼职刷单", "博彩", "赌博", "代开发票", "代开票", "办证", "套现",
    "跑分", "裸贷", "传销", "杀猪盘", "冒充公检法", "中奖诈骗", "境外来电",
    "验证码诈骗",
    # 广告 / 垃圾推广
    "加微信", "加qq", "扫码领", "点击链接领取", "免费领", "低价出售",
    "私聊我", "代购", "招代理",
)
_SENSITIVE_RE = re.compile("|".join(re.escape(w) for w in _SENSITIVE_WORDS), re.IGNORECASE)


def has_sensitive(text: str) -> bool:
    """判断文本是否命中敏感词（内置词表 + config 扩展词）。"""
    if not text:
        return False
    if _SENSITIVE_RE.search(text):
        return True
    extra = getattr(config, "QA_SENSITIVE_EXTRA_WORDS", set())
    if extra:
        # 动态读取 config，测试可随时 monkeypatch 扩展词表
        extra_re = re.compile("|".join(re.escape(w) for w in extra), re.IGNORECASE)
        if extra_re.search(text):
            return True
    return False


# ----------------------------------------------------------------------
# 2.5) 算法话题拦截：题解、思路、概念科普一律不答（省 token）
# ----------------------------------------------------------------------
# 判定规则：
#   - 队务类问题（课程/比赛/通知等，含“集训队”“校赛”“几点”等词）
#     优先放行，避免“集训队这周讲 DP 吗”被误伤；
#   - 命中任一“强词”（明确求解/要代码/调错意图）即拦截；
#   - 命中任一“编程主题词”即拦截——题解、概念科普（什么是 DP /
#     讲讲二分）一并拒答，不再放行科普问法；
#   - 部署方可用 config.QA_PROBLEM_EXTRA_WORDS 追加自定义强命中词。
_PROBLEM_STRONG_WORDS = (
    # 要解法 / 题解
    "题解", "解法", "解题思路", "求思路", "给个思路", "给个题解", "求题解", "要题解",
    # 求帮助解题 / 写代码
    "帮我解", "帮我做", "帮我写", "教我怎么做", "教我写", "帮我看看", "帮帮我",
    "求代码", "给代码", "完整代码", "代码怎么写", "怎么写代码", "帮我调", "帮我debug",
    # 报错 / 过不了
    "一直wa", "一直tle", "ac不了", "过不了", "超时了", "错在哪", "哪里错了", "为什么错",
    "卡住了", "卡在这", "样例过不了", "wa了", "tle了", "re了", "mle了",
    "答案是什么", "这题怎么做", "这道题怎么做",
    "不会做", "做不出来", "解不出来", "想不出来", "写不出来",
)
_PROBLEM_TOPIC_WORDS = (
    "算法", "代码", "程序", "递归", "动态规划", "dp", "dfs", "bfs", "二分",
    "贪心", "链表", "二叉树", "栈", "队列", "排序", "模拟", "枚举", "数组",
    "字符串", "图论", "最短路", "树", "哈希", "位运算", "回溯", "线段树",
    "树状数组", "并查集", "拓扑", "搜索", "分治", "双指针", "滑动窗口", "前缀和",
    "差分", "c++", "cpp", "python", "java", "leetcode", "力扣", "洛谷", "oj",
    "acm", "codeforces", "atcoder", "牛客", "vjudge",
)
# 队务信号词：命中即视为队务/组织问题，跳过算法话题拦截
_PROBLEM_AFFAIR_WORDS = (
    "集训", "集训队", "课程", "上课", "安排", "时间", "几点", "周几",
    "比赛", "报名", "校赛", "周赛", "选拔", "训练", "机房", "签到",
    "老师", "地点", "通知", "教室", "课表", "讲座", "例会", "招新", "出勤",
)
_PROBLEM_STRONG_RE = re.compile(
    "|".join(re.escape(w) for w in _PROBLEM_STRONG_WORDS), re.IGNORECASE
)
_PROBLEM_TOPIC_RE = re.compile(
    "|".join(re.escape(w) for w in _PROBLEM_TOPIC_WORDS), re.IGNORECASE
)
_PROBLEM_AFFAIR_RE = re.compile(
    "|".join(re.escape(w) for w in _PROBLEM_AFFAIR_WORDS), re.IGNORECASE
)


def is_algorithm_query(text: str) -> bool:
    """判断文本是否为算法话题提问（题解/思路/概念科普），命中则不调模型。

    队务类问题（课程、比赛、通知等）优先放行；算法相关提问一律拦截，
    包括“什么是动态规划”“讲讲二分思想”这类科普问法。
    """
    if not text:
        return False
    # 队务类问题优先放行，避免“集训队这周讲 DP 吗”被误伤
    if _PROBLEM_AFFAIR_RE.search(text):
        return False
    if _PROBLEM_STRONG_RE.search(text):
        return True
    extra = getattr(config, "QA_PROBLEM_EXTRA_WORDS", set())
    if extra:
        extra_re = re.compile("|".join(re.escape(w) for w in extra), re.IGNORECASE)
        if extra_re.search(text):
            return True
    # 命中编程主题词即拦截（含科普问法，不再放行）
    if _PROBLEM_TOPIC_RE.search(text):
        return True
    return False


# ----------------------------------------------------------------------
# 3) 输出过滤：二次敏感检查 + 长度截断 + 禁止超长链接
# ----------------------------------------------------------------------
_LINK_RE = re.compile(r"https?://\S+")


def sanitize_output(text: str, max_length: int = 800, max_link_length: int = 60) -> str:
    """清洗回复文本。

    顺序：先做二次敏感词检查（命中整段替换为过滤提示），
    再替换超长链接，最后做长度截断。三个步骤均为幂等操作，
    对已清洗文本重复调用不会产生额外副作用。
    """
    if not text:
        return text
    if config.QA_SENSITIVE_GATE_ENABLED and has_sensitive(text):
        return config.QA_OUTPUT_FILTERED_REPLY

    def _shorten_link(match: re.Match) -> str:
        url = match.group(0)
        return url if len(url) <= max_link_length else "[链接已省略]"

    text = _LINK_RE.sub(_shorten_link, text)
    if len(text) > max_length:
        text = text[:max_length] + "……"
    return text


# ----------------------------------------------------------------------
# 4) 频率防刷：滑动窗口限流
# ----------------------------------------------------------------------
class RateLimiter:
    """按 key（通常为 QQ 号）滑动窗口限流。

    窗口内累计调用超过 max_calls 次即拒绝，窗口自动滑动；历史记录随
    窗口过期自动清理，避免无限增长。
    """

    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._records: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """记录一次调用并返回是否允许；max_calls <= 0 表示不限流。"""
        if self.max_calls <= 0:
            return True
        if not key:
            # 无法识别调用方时按“放行”处理，避免误伤正常消息
            return True
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._records.get(key, []) if now - t < self.window_seconds]
            if len(hits) >= self.max_calls:
                self._records[key] = hits
                return False
            hits.append(now)
            self._records[key] = hits
            return True
