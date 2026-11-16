"""阶段三主流程接入测试：拟人闲聊、记忆积累与表情包顺风车在群消息链上的行为。

关注三件事：
  1. 现有 @机器人 指令 / 结构化问答 / 老师意图 / 复读机链路不被改变；
  2. 闲聊走 persona 后记忆被写入、上下文被回注，模型挂掉时优雅回退；
  3. 未 @ 的普通消息只在三重闸门全过时才主动插话。
"""

import sqlite3

import pytest

from cdutetc import chat_fun, config, database, main, memes, persona_chat
from conftest import official_message
from cdutetc.errors import DeliveryResult


def _msg(content="在吗", group="G-OPENID", member="m1", message_id=12345, at=True):
    """构造官方群消息；at=True 时生成正文 @ 与平台 is_you 标记。"""
    prefix = "<@BOT-OPENID> " if at else ""
    return official_message(message_type="group", content=prefix + content, group_id=group, user_id=member, msg_id=message_id, bot_id="BOT-OPENID")


class FakeQQBotClient:
    """官方协议客户端桩：只声明能力位，不联任何网。"""

    protocol = "qqbot"
    supports_passive_reply = True
    supports_numeric_group_id = False
    supports_markdown = True

    def __init__(self):
        from unittest.mock import AsyncMock

        self.upload_group_media = AsyncMock(return_value=("FILE-INFO-1", 1800))
        self.send_group_media_result = AsyncMock(
            return_value=DeliveryResult(True, "ok", "qqbot"),
        )


@pytest.fixture(autouse=True)
def _fresh_engines():
    chat_fun.reset_engine()
    memes.reset_engine()
    persona_chat.reset_engine()
    yield
    chat_fun.reset_engine()
    memes.reset_engine()
    persona_chat.reset_engine()


@pytest.fixture
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture
def bot(monkeypatch, db_conn):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"G-OPENID"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"m1"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "ECHO_ENABLED", True)
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")

    client = FakeQQBotClient()
    instance = main.ACMBot(client)
    replies = []

    async def fake_reply(message, content):
        replies.append(content)
        return True

    monkeypatch.setattr(instance, "_safe_reply", fake_reply)
    return instance, client, replies


def _patch_llm(monkeypatch, reply="这题我也不会，先补个线段树 [MEME:ac]"):
    """把模型调用换成固定回包，并记录每次送入的 Prompt。"""
    calls = []

    async def fake_chat(system, user, **kwargs):
        calls.append((system, user))
        return reply

    monkeypatch.setattr(persona_chat, "chat", fake_chat)
    return calls


# ---------- @机器人闲聊 ----------


async def test_social_message_goes_to_persona(bot, monkeypatch):
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch)
    await b.on_group_at_message_create(_msg("在吗"))
    assert replies == ["这题我也不会，先补个线段树"]
    assert len(calls) == 1
    system, user = calls[0]
    assert "学姐" in system
    assert "在吗" in user


async def test_persona_uses_daily_state_and_context(bot, monkeypatch):
    b, _client, replies = bot
    database.set_daily_state("刚点完外卖，正在B站看算法区UP主整活")
    memory = persona_chat.get_memory()
    memory.observe("G-OPENID", "小明", "今晚补题吗")
    calls = _patch_llm(monkeypatch, reply="补，等我先把这道DP抄一遍")
    await b.on_group_at_message_create(_msg("学姐在干嘛"))
    system, user = calls[0]
    assert "刚点完外卖，正在B站看算法区UP主整活" in system
    assert "[小明]: 今晚补题吗" in user
    assert replies == ["补，等我先把这道DP抄一遍"]


async def test_persona_writes_memory_fact_without_llm(bot, monkeypatch):
    """命中「我叫/我擅长」等模板：先落库，再带着这条印象去回话。"""
    b, _client, _replies = bot
    calls = _patch_llm(monkeypatch, reply="记住了，线段树大佬")
    await b.on_group_at_message_create(_msg("我擅长线段树"))
    assert database.list_memory_facts("G-OPENID", "m1") == ["我擅长线段树"]
    _system, user = calls[0]
    assert "我擅长线段树" in user


async def test_persona_memory_fact_at_most_five_rows(bot, monkeypatch):
    b, _client, _replies = bot
    _patch_llm(monkeypatch, reply="行")
    for index in range(8):
        await b.on_group_at_message_create(_msg(f"我叫学员{index}"))
    facts = database.list_memory_facts("G-OPENID", "m1", limit=99)
    assert len(facts) == database.MEMORY_FACTS_KEEP
    assert "我叫学员7" in facts


async def test_meme_tag_sends_meme_with_same_msg_id(bot, monkeypatch, tmp_path):
    """回复带 [MEME:ac]：先发纯文本，再用同一条 msg_id 顺带发图。"""
    b, client, replies = bot
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")
    _use_assets(monkeypatch, _assets(tmp_path))
    _patch_llm(monkeypatch, reply="这题我也不会 [MEME:ac]")
    await b.on_group_at_message_create(_msg("在吗", message_id=98765))
    assert replies == ["这题我也不会"]
    client.upload_group_media.assert_awaited_once()
    assert client.send_group_media_result.await_count == 1
    kwargs = client.send_group_media_result.await_args.kwargs
    assert kwargs["reply_to"] == "98765"


async def test_meme_tag_without_meme_enabled_only_sends_text(bot, monkeypatch):
    b, client, replies = bot
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    _patch_llm(monkeypatch, reply="摸鱼中 [MEME:slacking]")
    await b.on_group_at_message_create(_msg("学姐摸鱼"))
    assert replies == ["摸鱼中"]
    client.upload_group_media.assert_not_awaited()


async def test_meme_tag_stripped_even_without_base_url(bot, monkeypatch):
    """没配素材地址时退化成纯文本，标签绝不泄漏到群里。"""
    b, client, replies = bot
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    _patch_llm(monkeypatch, reply="走着 [MEME:interrupt]")
    await b.on_group_at_message_create(_msg("学姐出来"))
    assert replies == ["走着"]
    client.upload_group_media.assert_not_awaited()


async def test_customer_tone_reply_is_dropped(bot, monkeypatch):
    """模型走客服腔：整条丢弃，绝不发到群里。"""
    b, _client, replies = bot
    _patch_llm(monkeypatch, reply="作为AI助手，很高兴为您服务")
    await b.on_group_at_message_create(_msg("在吗"))
    assert all("作为AI助手" not in reply for reply in replies)


async def test_llm_failure_falls_back_to_original_reply(bot, monkeypatch):
    """模型异常：退回 commands 的原有兜底，聊天功能挂掉也不影响可用性。"""
    b, _client, replies = bot

    async def boom(system, user, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr(persona_chat, "chat", boom)
    await b.on_group_at_message_create(_msg("在吗"))
    assert len(replies) == 1
    assert replies[0]


async def test_persona_disabled_keeps_original_social_reply(bot, monkeypatch):
    b, _client, replies = bot
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    calls = _patch_llm(monkeypatch)
    await b.on_group_at_message_create(_msg("在吗"))
    assert calls == []
    assert len(replies) == 1 and replies[0]


async def test_missing_api_key_keeps_original_reply(bot, monkeypatch):
    """未配 Key：persona.chat 返回空串 → 回到原有兜底。"""
    b, _client, replies = bot
    monkeypatch.setattr(config, "LLM_API_KEY", "")

    async def real_chat(system, user, **kwargs):
        return await persona_chat.chat.__wrapped__(system, user) \
            if hasattr(persona_chat.chat, "__wrapped__") else ""

    await b.on_group_at_message_create(_msg("在吗"))
    # 真实 chat() 无 Key 直接返回空串（未被替换），因此仍是原有兜底
    assert len(replies) == 1 and replies[0]


# ---------- 现有功能不受影响 ----------


async def test_slash_command_still_works(bot, monkeypatch):
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch)
    await b.on_group_at_message_create(_msg("/比赛"))
    assert calls == []
    assert replies  # 命令原样回复，没有走 persona


async def test_information_request_still_answered_by_commands(bot, monkeypatch):
    """真正的队务提问不进 persona（只接管寒暄/接梗）。"""
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch)
    await b.on_group_at_message_create(_msg("这周有什么比赛吗"))
    assert calls == []
    assert replies


async def test_teacher_intent_still_consumes_message(bot, monkeypatch):
    b, _client, replies = bot
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)
    calls = _patch_llm(monkeypatch)

    async def fake_handle(text, **kwargs):
        return {"action": "saved", "reply": "已登记"}

    monkeypatch.setattr(main.teacher_intent, "handle_message", fake_handle)
    await b.on_group_at_message_create(_msg("周五晚上七点开课", member="m1", at=False))
    assert calls == []
    assert replies == ["已登记"]


async def test_echo_still_wins_before_interjection(bot, monkeypatch):
    """复读机命中时由复读回应，不再叠加主动插话。"""
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="学姐来了")
    for member in ("m1", "m2"):
        await b.on_group_at_message_create(_msg("破防了", member=member, at=False))
    await b.on_group_at_message_create(_msg("破防了", member="m3", at=False))
    assert replies == ["破防了"]
    assert calls == []


# ---------- 群体高潮插话 ----------


def _gate_hits(monkeypatch):
    """把插话闸门的随机源固定为必中（条数与冷却仍按真实逻辑）。"""
    gate = persona_chat.get_gate()
    monkeypatch.setattr(gate, "_rng", lambda: 0.0)
    return gate


async def test_interjection_silent_without_high_tide(bot, monkeypatch):
    """群消息不足 12 条：即便概率命中也不插话。"""
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="学姐来了")
    _gate_hits(monkeypatch)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES - 1):
        await b.on_group_at_message_create(_msg(f"闲聊{index}", member=f"m{index}", at=False))
    assert replies == []
    assert calls == []


async def test_interjection_fires_on_high_tide(bot, monkeypatch):
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="你们这是又集体红温了")
    _gate_hits(monkeypatch)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES):
        await b.on_group_at_message_create(_msg(f"闲聊{index}", member=f"m{index}", at=False))
    assert replies == ["你们这是又集体红温了"]
    assert len(calls) == 1


async def test_interjection_respects_cooldown(bot, monkeypatch):
    """一次插话后 60 分钟内不再插话，哪怕群又炸了。"""
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="安静点")
    _gate_hits(monkeypatch)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES):
        await b.on_group_at_message_create(_msg(f"第一波{index}", member=f"m{index}", at=False))
    assert replies == ["安静点"]
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES * 2):
        await b.on_group_at_message_create(_msg(f"第二波{index}", member=f"m{index}", at=False))
    assert replies == ["安静点"]
    assert len(calls) == 1


async def test_interjection_probability_miss_keeps_quiet(bot, monkeypatch):
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="学姐来了")
    gate = persona_chat.get_gate()
    monkeypatch.setattr(gate, "_rng", lambda: 0.99)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES * 2):
        await b.on_group_at_message_create(_msg(f"闲聊{index}", member=f"m{index}", at=False))
    assert replies == []
    assert calls == []


async def test_interjection_disabled_by_switch(bot, monkeypatch):
    b, _client, replies = bot
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    calls = _patch_llm(monkeypatch, reply="学姐来了")
    _gate_hits(monkeypatch)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES * 2):
        await b.on_group_at_message_create(_msg(f"闲聊{index}", member=f"m{index}", at=False))
    assert replies == []
    assert calls == []


async def test_interjection_ignores_slash_commands(bot, monkeypatch):
    """/ 命令永远不参与插话统计，也不会因为群热闹被顶掉。"""
    b, _client, replies = bot
    calls = _patch_llm(monkeypatch, reply="学姐来了")
    _gate_hits(monkeypatch)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES * 2):
        await b.on_group_at_message_create(_msg("/帮助"))
    assert calls == []
    assert replies


# ---------- Prompt 预算与昵称 ----------


async def test_prompt_stays_within_token_budget_on_busy_group(bot, monkeypatch):
    b, _client, _replies = bot
    for index in range(30):
        await b.on_group_at_message_create(_msg(f"第{index}条很长的闲聊" * 5, member=f"m{index}", at=False))
    calls = _patch_llm(monkeypatch, reply="行")
    await b.on_group_at_message_create(_msg("在吗，学姐"))
    system, user = calls[0]
    total = persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(user)
    assert total <= persona_chat.PROMPT_TOKEN_BUDGET


async def test_nickname_falls_back_to_member_tail(bot, monkeypatch):
    b, _client, _replies = bot
    calls = _patch_llm(monkeypatch, reply="行")
    await b.on_group_at_message_create(_msg("在吗", member="123456789"))
    _system, user = calls[0]
    assert "群友6789" in user


def _use_assets(monkeypatch, root):
    engine = memes.MemeEngine(root=root, rng=lambda: 0.0)
    monkeypatch.setattr(memes, "_default_engine", engine)
    return engine


def _assets(root):
    for category in memes.CATEGORIES:
        directory = root / category
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "1.png").write_bytes(b"fake-png")
    return root
