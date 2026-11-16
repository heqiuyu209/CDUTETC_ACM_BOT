"""阶段三收尾：记忆去重与上限、每日状态注入、拟人插话防刷屏。

覆盖范围：
  - 写入侧：同一条标签重复出现只刷新时间，不额外占槽位；
  - 读取侧：历史脏重复行先去重再截断，不能挤掉真正有效的旧标签；
  - 提取侧：疑问句/一次性水聊不进记忆（避免「我是不是该退役了」变永久标签）；
  - 每日状态：写进 system prompt、只出现一次、缺失时回退默认、语料不自我介绍；
  - 印象注入：自然融入对话，不带「记忆库/记录显示」这类出戏词；
  - 插话节奏：机器人刚说过话后的静默窗、句数/字数压缩、与娱乐模块共用时间线。
"""

from __future__ import annotations

import inspect

import pytest

from cdutetc import config, database as db, main, persona_chat
import cdutetc.bot.persona as bot_persona


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    """隔离的临时库：每个用例独立一份，互不污染。"""
    db_path = tmp_path / "persona.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DB_PATH", db_path, raising=False)
    db.init_db()
    yield db_path
    try:
        db_path.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def isolated_persona(tmp_path, monkeypatch):
    """人设文件指向临时目录，用例绝不碰仓库里的 assets/persona.txt。"""
    path = tmp_path / "persona.txt"
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", path)
    persona_chat.reset_persona_cache()
    yield path
    persona_chat.reset_persona_cache()
    persona_chat.reset_engine()


# ---------------- 记忆：写入侧去重与上限 ----------------

def test_same_fact_refreshes_instead_of_taking_new_slot(memory_db):
    """「我叫小明」刷十次也只占一个槽位。"""
    for _ in range(10):
        assert db.add_memory_fact("g1", "u1", "我叫小明")
    facts = db.list_memory_facts("g1", "u1", limit=10)
    assert facts == ["我叫小明"]


def test_cap_keeps_newest_and_drops_oldest(memory_db):
    """超过上限时淘汰最旧：留下的应是最新 5 条，且顺序为倒序。"""
    for index in range(8):
        assert db.add_memory_fact("g1", "u1", f"标签{index}")
    facts = db.list_memory_facts("g1", "u1", limit=10)
    assert len(facts) == db.MEMORY_FACTS_KEEP
    assert facts == [f"标签{7}", f"标签{6}", f"标签{5}", f"标签{4}", f"标签{3}"]


def test_dedupe_happens_before_limit_so_valid_rows_survive(memory_db):
    """历史脏数据（同标签多行）去重后仍要回满 limit，不能被挤空。"""
    with db._connect() as conn:
        rows = [
            ("g1", "u1", "我叫小明", "2026-11-16 10:00:00"),
            ("g1", "u1", "我叫小明", "2026-11-16 09:00:00"),
            ("g1", "u1", "我叫小明", "2026-11-16 08:00:00"),
            ("g1", "u1", "我擅长线段树", "2026-11-15 10:00:00"),
            ("g1", "u1", "我打过省赛", "2026-11-14 10:00:00"),
        ]
        conn.executemany(
            "INSERT INTO bot_memory_facts (group_openid, user_openid, fact, updated_at)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
    facts = db.list_memory_facts("g1", "u1", limit=3)
    assert facts == ["我叫小明", "我擅长线段树", "我打过省赛"]


@pytest.mark.parametrize("text", [
    "我叫什么名字来着",
    "我是不是该退役了",
    "我是来问一下报名的事",
    "我擅长DP吗？",
])
def test_transient_chatter_is_not_memorized(text):
    """疑问、试探、一次性提问一律不收：这些沉淀下来只会污染印象。"""
    assert persona_chat.extract_memory_fact(text) == ""


@pytest.mark.parametrize("text,expected", [
    ("我擅长线段树", "我擅长线段树"),
    ("我叫小明", "我叫小明"),
    ("我AC了这场 div2", "我AC了这场"),
])
def test_stable_traits_are_still_memorized(text, expected):
    assert persona_chat.extract_memory_fact(text) == expected


# ---------------- 每日状态：注入方式 ----------------

def test_daily_state_lands_in_system_prompt_exactly_once(memory_db):
    status = "正在整理今晚的补题清单"
    assert db.set_daily_state(status)
    system, _ = persona_chat.build_conversation(
        nickname="小明", text="在吗", status_text=db.get_daily_state()["status_text"])
    assert system.count(status) == 1


def test_daily_state_falls_back_to_default_when_missing(memory_db):
    """库里没有记录时自动初始化默认状态，人设里也照此渲染。"""
    status = db.get_daily_state()["status_text"]
    assert status == db.DEFAULT_DAILY_STATUS
    system, _ = persona_chat.build_conversation(nickname="小明", text="在吗", status_text="")
    assert db.DEFAULT_DAILY_STATUS in system


def test_daily_status_pool_never_introduces_itself_and_stays_short():
    """状态是背景，不是自我介绍：不能出现机器人身份、客服腔，也别写成小作文。"""
    banned = ("我是", "机器人", "助手", "您好", "为您服务", "AI", "。")
    for status in persona_chat.DAILY_STATUS_POOL:
        assert len(status) <= 30, status
        for word in banned:
            assert word not in status, f"{status} 含出戏词 {word}"


# ---------------- 印象注入：自然融入，不出戏 ----------------

def test_fact_injection_stays_in_character(memory_db):
    system, user = persona_chat.build_conversation(
        nickname="小明", text="这题怎么又WA了", status_text=db.DEFAULT_DAILY_STATUS,
        facts=["我擅长线段树", "我叫小明"])
    assert "我擅长线段树" in user
    assert "你记得这个群友" in user
    for outing in ("记忆数据库", "根据记录显示", "据我记录"):
        assert outing not in user
        assert outing not in system


def test_meme_tag_and_service_tone_never_reach_the_group():
    assert persona_chat.clean_reply("您好，很高兴为您服务") == ""
    assert persona_chat.clean_reply("根据我的记忆库显示，你上次说过") == ""
    text, category = persona_chat.split_meme_tag("这题我熟 [MEME:ac]")
    assert (text, category) == ("这题我熟", "ac")


# ---------------- 主动插话：防刷屏 ----------------

def _armed_gate(monkeypatch):
    """概率置 1 的闸门单例：只留静默窗/冷却两道闸，便于验证防刷屏。"""
    persona_chat.reset_engine()
    gate = persona_chat.get_gate()
    # 概率是构造参数默认值（函数定义时已绑定常量），要改实例属性才生效。
    monkeypatch.setattr(gate, "probability", 1.0)
    return gate


def test_quiet_window_silences_interjection_after_bot_spoke(monkeypatch):
    """复读或表情包刚发过言，插话就得让路——同一条消息不能引出好几句。"""
    gate = _armed_gate(monkeypatch)
    bot = main.ACMBot()
    bot._note_bot_spoke("10001")
    results = [gate.observe("10001") for _ in range(persona_chat.INTERJECT_MIN_MESSAGES * 2)]
    assert not any(results)


def test_interjection_still_fires_once_when_bot_kept_quiet(monkeypatch):
    """反例：机器人没说话时，闸门满足条件仍应插一次话。"""
    gate = _armed_gate(monkeypatch)
    results = [gate.observe("10002") for _ in range(persona_chat.INTERJECT_MIN_MESSAGES)]
    assert results.count(True) == 1


def test_interjection_after_spoke_is_blocked_then_recovers(monkeypatch):
    """静默窗过期后闸门恢复工作（时钟注入）。"""
    clock = {"now": 1000.0}
    gate = persona_chat.InterjectionGate(
        probability=1.0, clock=lambda: clock["now"], rng=lambda: 0.0)
    gate.note_bot_spoke("g1")
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES):
        assert gate.observe("g1") is False
    clock["now"] += persona_chat.INTERJECT_QUIET_SECONDS + 1
    results = [gate.observe("g1") for _ in range(persona_chat.INTERJECT_MIN_MESSAGES)]
    assert results.count(True) == 1


@pytest.mark.parametrize("raw,expected", [
    ("这题WA了？我也是。补题吧。明天一起打。", "这题WA了？我也是。"),
    ("好", "好"),
    ("", ""),
    ("   ", ""),
])
def test_clamp_reply_keeps_only_leading_sentences(raw, expected):
    assert persona_chat.clamp_reply(
        raw,
        max_chars=persona_chat.INTERJECT_MAX_CHARS,
        max_sentences=persona_chat.INTERJECT_MAX_SENTENCES,
    ) == expected


def test_clamp_reply_hard_truncates_without_trailing_punctuation():
    body = "一" * (persona_chat.INTERJECT_MAX_CHARS + 20) + "，"
    clamped = persona_chat.clamp_reply(body, max_chars=persona_chat.INTERJECT_MAX_CHARS)
    assert len(clamped) == persona_chat.INTERJECT_MAX_CHARS
    assert clamped[-1] != "，"


def test_interjection_budget_is_stricter_than_normal_reply():
    assert 0 < persona_chat.INTERJECT_MAX_CHARS < persona_chat.REPLY_MAX_CHARS
    assert persona_chat.INTERJECT_MAX_SENTENCES <= 2
    assert persona_chat.INTERJECT_QUIET_SECONDS > 0


def test_entertainment_and_interjection_share_one_speaking_timeline():
    """复读/表情包分支必须登记发言，插话路径必须带上截断参数。"""
    chat_fun_src = inspect.getsource(bot_persona.PersonaMixin._handle_chat_fun)
    interject_src = inspect.getsource(bot_persona.PersonaMixin._maybe_persona_interject)
    reply_src = inspect.getsource(bot_persona.PersonaMixin._handle_persona_chat)
    assert "_note_bot_spoke" in chat_fun_src
    assert "INTERJECT_MAX_CHARS" in interject_src
    assert "INTERJECT_MAX_SENTENCES" in interject_src
    # 真正的截断在回复链路里做：插话只是把更紧的额度传下去。
    assert "clamp_reply" in reply_src
    assert "max_chars" in reply_src and "max_sentences" in reply_src
