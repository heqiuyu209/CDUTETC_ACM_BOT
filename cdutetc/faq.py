"""本群 FAQ 检索：本地意图区分、候选排序和可选的低成本模型消歧。"""

import asyncio
from collections import OrderedDict
from functools import lru_cache
import re
import time
import unicodedata

from cdutetc import config, content_filter, database, llm

_ALIASES = (
    ("笔记本电脑", "电脑"), ("笔记本", "电脑"),
    ("什么时候", "时间"), ("啥时候", "时间"), ("何时", "时间"),
    ("在哪里", "在哪"), ("在哪儿", "在哪"), ("哪里", "在哪"),
    ("如何", "怎么"), ("怎样", "怎么"), ("咋", "怎么"),
    ("这星期", "本周"), ("这礼拜", "本周"), ("这周", "本周"),
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


@lru_cache(maxsize=4096)
def _features(text: str) -> tuple[str, str, frozenset[str]]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    for source, target in _ALIASES:
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"[^\w+#]+|_", "", normalized)
    intents = frozenset(name for name, pattern in _INTENTS.items() if re.search(pattern, normalized))
    return normalized, _NOISE.sub("", normalized), intents


def _score(question: str, stored: str) -> float:
    query, topic, intents = _features(question)
    candidate, other_topic, other_intents = _features(stored)
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    # 同一主题的“几点”和“在哪”不能互相充当答案。
    if intents and other_intents and not intents.intersection(other_intents):
        return 0.0
    if len(intents) > 1 and other_intents and not intents.issubset(other_intents):
        return 0.0
    for words in (("本周", "下周", "上周"), ("报名", "开赛")):
        left = {word for word in words if word in query}
        right = {word for word in words if word in candidate}
        if left and right and left.isdisjoint(right):
            return 0.0
    if not topic or not other_topic:
        return 0.0
    topic_score = content_filter.text_similarity(topic, other_topic)
    if topic_score == 0:
        return 0.0
    score = 0.55 * content_filter.text_similarity(query, candidate) + 0.45 * topic_score
    if len(topic) >= 2 and (topic in other_topic or other_topic in topic):
        score = max(score, 0.80)
    if intents and intents == other_intents:
        score = min(0.98, score + 0.08)
    return score


def rank(question: str, faqs: list[dict]) -> list[tuple[float, dict]]:
    """只按问题排序，答案不参与召回，避免答案中的泛词拉错主题。"""
    scored = [(_score(question, faq["question"]), faq) for faq in faqs]
    return sorted((item for item in scored if item[0] >= 0.20), key=lambda item: item[0], reverse=True)


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


async def find(group_id: str, question: str, faqs: list[dict]) -> dict | None:
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
    key = (group_id, _features(question)[0],
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
