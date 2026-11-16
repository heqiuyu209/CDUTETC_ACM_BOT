"""本群 FAQ 检索：本地意图区分、候选排序和可选的低成本模型消歧。"""

import asyncio
import re
import time
import unicodedata
from collections import OrderedDict
from functools import lru_cache

from cdutetc import config, content_filter, database, llm

_ALIASES = (
    ("笔记本电脑", "电脑"), ("笔记本", "电脑"),
    ("什么时候", "时间"), ("啥时候", "时间"), ("何时", "时间"),
    ("在哪里", "在哪"), ("在哪儿", "在哪"), ("哪里", "在哪"),
    ("如何", "怎么"), ("怎样", "怎么"), ("咋", "怎么"),
    ("这星期", "本周"), ("这礼拜", "本周"), ("这周", "本周"),
    ("截止时间", "截止"), ("报名截止", "截止"), ("最后期限", "截止"),
    ("要不要带", "带"), ("需要带", "带"), ("用不用带", "带"),
    ("可不可以", "可以"), ("能不能", "可以"),
    ("qq群", "qq"),
)
_INTENTS = {
    "time": r"时间|几点|几号|哪天|周几|星期几|截止|最晚",
    "place": r"在哪|地点|位置|哪个教室|哪个机房|何处",
    "method": r"怎么|流程|步骤|操作",
    "requirement": r"要求|条件|资格|需要|要不要|带.+吗|能.+吗|可以.+吗",
    "cost": r"费用|多少钱|收费|价格",
    "contact": r"联系|找谁|负责人",
    "cancel": r"取消|退出|撤销|请假",
}
_NOISE = re.compile(
    r"请问|问一下|麻烦|时间|几点|几号|哪天|在哪|地点|位置|怎么|流程|步骤|"
    r"要求|条件|需要|要不要|是否|有没有|有没|是不是|可以|本周|这个|一下|"
    r"[吗呢呀啊的了有是要带]"
)
_OBJECTS = re.compile(r"校赛|周赛|选拔赛|训练赛|蓝桥杯|天梯赛|icpc|ccpc|codeforces|atcoder")
_SCOPE_NOISE = re.compile(r"比赛|报名|参赛|参加|开始|开赛|截止|举办|进行|什么|条件|要求|费用|流程|方法|材料")

# 只有泛词、没有实际主题的问法（「怎么办」「什么情况」），去掉噪词后几乎不剩信息，
# 让它们参与打分只会随机撞上某条 FAQ，因此直接放弃召回。
_GENERIC_TOPICS = frozenset({"怎么办", "什么情况", "啥情况", "相关", "问题", "事项", "情况"})
# 核心词不足 2 个字符时同样没有区分度（「带」「吗」），不做模糊召回。
MIN_TOPIC_CHARS = 2
# 打分权重：核心词（去掉问句噪词后剩下的主题词）比整句字面更可信，占比更高。
WEIGHT_FULL = 0.45
WEIGHT_TOPIC = 0.55
# 进入候选池的最低分；低于此分不进排名，也绝不会被当作命中。
RANK_FLOOR = 0.20


@lru_cache(maxsize=4096)
def _features(text: str) -> tuple[str, str, frozenset[str]]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    for source, target in _ALIASES:
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"(?:星期|礼拜)([一二三四五六日天])", r"周\1", normalized).replace("周天", "周日")
    normalized = re.sub(r"[^\w+#]+|_", "", normalized)
    intents = frozenset(name for name, pattern in _INTENTS.items() if re.search(pattern, normalized))
    return normalized, _NOISE.sub("", normalized), intents


def _compatible(question: str, stored: str) -> bool:
    """排除明确不同的对象、日期、否定条件及所问时间类型。"""
    query, topic, intents = _features(question)
    candidate, other_topic, other_intents = _features(stored)
    if intents and not intents.issubset(other_intents):
        return False
    objects, other_objects = set(_OBJECTS.findall(query)), set(_OBJECTS.findall(candidate))
    if objects and other_objects and objects != other_objects:
        return False
    for pattern in (r"周[一二三四五六日]", r"上周|下周|本周", r"国赛|省赛|初赛|决赛"):
        left, right = set(re.findall(pattern, query)), set(re.findall(pattern, candidate))
        if left and right and left != right:
            return False
    def numbers(text):
        return tuple(value.lstrip("0") or "0" for value in re.findall(
            r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", text),
        ))
    if numbers(question) != numbers(stored):
        return False
    if bool(re.search(r"不|无需|无须|禁止", query)) != bool(re.search(r"不|无需|无须|禁止", candidate)):
        return False
    if "time" in intents:
        def time_kind(text):
            if "截止" in text or "最晚" in text:
                return "deadline"
            return "registration" if "报名" in text else "start"
        if time_kind(query) != time_kind(candidate):
            return False
    # 报名等通用词不应让两个不同的自定义比赛名称获得高分。
    scope, other_scope = _SCOPE_NOISE.sub("", topic), _SCOPE_NOISE.sub("", other_topic)
    if scope and other_scope and content_filter.text_similarity(scope, other_scope) < 0.40:
        return False
    return True


def _score(question: str, stored: str) -> float:
    query, topic, intents = _features(question)
    candidate, other_topic, other_intents = _features(stored)
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0 if _compatible(question, stored) else 0.0
    if not _compatible(question, stored):
        return 0.0
    # 去噪后没有实际主题（或只剩一两个字）时不给分：短词误触发基本都出在这里。
    if len(topic) < MIN_TOPIC_CHARS or len(other_topic) < MIN_TOPIC_CHARS:
        return 0.0
    if topic in _GENERIC_TOPICS or other_topic in _GENERIC_TOPICS:
        return 0.0
    topic_score = content_filter.text_similarity(topic, other_topic)
    if topic_score == 0:
        return 0.0
    score = WEIGHT_FULL * content_filter.text_similarity(query, candidate) + WEIGHT_TOPIC * topic_score
    # 核心词互相包含说明问的是同一事项（「训练时间」vs「周三训练的时间」）。
    if len(topic) >= 2 and (topic in other_topic or other_topic in topic):
        score = max(score, 0.80)
    if intents and intents == other_intents:
        score = min(0.98, score + 0.08)
    # 数字完全一致（同一天、同一个人数）是把同一件事问法不同的强信号，小幅加分。
    digits = re.findall(r"\d+", query)
    if digits and digits == re.findall(r"\d+", candidate):
        score = min(0.99, score + 0.05)
    return score


def rank(question: str, faqs: list[dict]) -> list[tuple[float, dict]]:
    """只按问题排序，答案不参与召回，避免答案中的泛词拉错主题。"""
    scored = [(_score(question, faq["question"]), faq) for faq in faqs]
    return sorted((item for item in scored if item[0] >= RANK_FLOOR),
                  key=lambda item: item[0], reverse=True)


def top_matches(question: str, faqs: list[dict], *, limit: int = 3) -> list[tuple[float, dict]]:
    """得分最高的若干条候选，供问答提示词注入参考事实。

    与 :func:`local_match` 的区别：这里不做「能不能直接作答」的判断，只负责取证据，
    因此即便整体置信度不高，只要语义相关就交给模型结合事实组织语言。
    """
    if limit <= 0:
        return []
    return rank(question, faqs)[:limit]


def local_match(ranked: list[tuple[float, dict]]) -> dict | None:
    if not ranked:
        return None
    score, best = ranked[0]
    if score == 1.0:
        return best
    if score < max(0.60, config.QA_KB_MATCH_MIN_SCORE):
        return None
    # 两条相近候选有不同答案时，不依赖数据库顺序猜一个。
    if len(ranked) > 1 and score - ranked[1][0] < 0.10 and best["answer"] != ranked[1][1]["answer"]:
        return None
    return best


_CACHE_LIMIT = 256
_CACHE_SECONDS = 300
_choices: OrderedDict[tuple, tuple[float, int]] = OrderedDict()
_inflight: dict[tuple, asyncio.Task] = {}


async def find(group_id: str, question: str, faqs: list[dict], *, exact_only: bool = False) -> dict | None:
    if exact_only:
        # 非问句只允许完整命中已录入的词条，既不模糊猜测，也不调用模型。
        normalized = _features(question)[0]
        matches = [faq for faq in faqs if normalized and _features(faq["question"])[0] == normalized]
        return matches[0] if matches and len({faq["answer"] for faq in matches}) == 1 else None
    ranked = rank(question, faqs)
    matched = local_match(ranked)
    if matched is not None or not config.QA_FAQ_LLM_ENABLED:
        return matched
    if (not ranked or not content_filter.is_topic_relevant(question)
            or content_filter.is_algorithm_query(question)
            or content_filter.is_capability_query(question)
            or (config.QA_SENSITIVE_GATE_ENABLED and content_filter.has_sensitive(question))):
        return None
    candidates = [faq for _, faq in ranked[:5] if len(faq["question"]) <= 100]
    if not candidates or len(question) > 160:
        return None
    # 答案也纳入版本：老师在同一秒修改资料仍会立即失效，群之间不复用。
    key = (group_id, unicodedata.normalize("NFKC", question).strip(),
           tuple((faq["id"], faq["question"], faq["answer"]) for faq in candidates))
    cached = _choices.get(key)
    if cached and cached[0] > time.monotonic():
        _choices.move_to_end(key)
        selected = cached[1]
    else:
        task = _inflight.get(key)
        if task is None:
            if len(_inflight) >= 64:
                return None
            async def select():
                selected = await llm.choose_faq(question, candidates)
                if selected is not None:
                    _choices[key] = (time.monotonic() + _CACHE_SECONDS, selected)
                    _choices.move_to_end(key)
                    while len(_choices) > _CACHE_LIMIT:
                        _choices.popitem(last=False)
                return selected
            task = asyncio.create_task(select())
            _inflight[key] = task
            task.add_done_callback(lambda done: _inflight.pop(key, None))
        selected = await asyncio.shield(task)
    chosen = next((faq for faq in candidates if faq["id"] == selected), None)
    if chosen is None:
        return None
    # 等待模型期间老师可能删改 FAQ；重新按群/编号取当前有效记录。
    current = database.get_faq(group_id, selected)
    return current if current and current["question"] == chosen["question"] else None
