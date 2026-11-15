"""阶段三：低 Token 拟人化闲聊、三层记忆与每日状态机。

覆盖范围：
  - Prompt 预算：任意长度上下文下总输入 Token 不超过 700；
  - 每日状态机：三档时刻、随机切换、无记录时读取自动初始化默认状态；
  - [MEME:目录] 剥离与分类校验（含非法目录、大小写、中文冒号）；
  - 个人记忆：0 Token 规则提取、写入后回注、每对上限 5 条淘汰最旧；
  - 高潮插话闸门：条数、冷却、概率三重条件与重置；
  - 回复清洗：客服腔整条丢弃、角色前缀剥离、长度截断。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from cdutetc import config, database as db, persona_chat


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
    """人设文件指向临时目录：用例绝不碰仓库里的 assets/persona.txt。"""
    path = tmp_path / "persona.txt"
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", path)
    persona_chat.reset_persona_cache()
    yield path
    persona_chat.reset_persona_cache()


# ---------------- 外部人设文件 ----------------

def test_persona_file_generated_with_default_when_missing(isolated_persona):
    """文件不存在时：自动创建并写入默认吉祥物学姐人设。"""
    assert not isolated_persona.exists()
    system = persona_chat._system_prompt("刚点完外卖")
    assert isolated_persona.is_file()
    saved = isolated_persona.read_text(encoding="utf-8")
    assert "{status}" in saved and "学姐" in saved
    assert "刚点完外卖" in system
    assert "学姐" in system
    # 文件里的 # 说明行只给人看，不进 Prompt
    assert "热更新：改完保存即生效" not in system


def test_persona_file_hot_reload_without_restart(isolated_persona):
    """改完文件立刻生效：不动进程、不调二次初始化。"""
    isolated_persona.parent.mkdir(parents=True, exist_ok=True)
    isolated_persona.write_text("第一版人设：你是学姐。\n状态：{status}\n", encoding="utf-8")
    assert "第一版人设" in persona_chat._system_prompt("在机房")

    isolated_persona.write_text("第二版人设：你是暴躁老哥，短句输出。\n状态：{status}\n",
                                encoding="utf-8")
    second = persona_chat._system_prompt("在机房")
    assert "暴躁老哥" in second
    assert "第一版人设" not in second
    assert "在机房" in second


def test_persona_without_status_placeholder_appends_status_line(isolated_persona):
    isolated_persona.write_text("你是学姐，说话很短。", encoding="utf-8")
    system = persona_chat._system_prompt("正在补题")
    assert "你现在的生活状态：正在补题" in system


def test_empty_persona_file_regenerated_with_default(isolated_persona):
    """只写了注释 / 全空白的文件按「没配置」处理：回退默认并重写。"""
    isolated_persona.write_text("  \n# 只有注释\n", encoding="utf-8")
    system = persona_chat._system_prompt("摸鱼中")
    assert "学姐" in system
    assert "{status}" in isolated_persona.read_text(encoding="utf-8")


def test_unreadable_persona_file_falls_back_to_default(tmp_path):
    """路径读不出来（这里是目录）也不该炸：用默认人设继续聊天。"""
    broken = tmp_path / "persona_dir.txt"
    broken.mkdir()
    assert "学姐" in persona_chat.load_persona(broken)


def test_huge_persona_still_respects_token_budget(isolated_persona):
    """人设写成长篇大论也不许顶爆预算：状态行优先保留，其余按需砍掉。"""
    isolated_persona.write_text("人设废话。" * 500 + "\n状态：{status}\n", encoding="utf-8")
    system = persona_chat._system_prompt("在机房摸鱼")
    assert persona_chat.estimate_tokens(system) <= persona_chat.SYSTEM_TOKEN_LIMIT
    assert "在机房摸鱼" in system
    full_system, user = persona_chat.build_conversation(
        nickname="小明", text="学姐这题怎么写" * 20,
        status_text="在机房摸鱼", context=["[群友]: 又TLE了"] * 20,
    )
    total = persona_chat.estimate_tokens(full_system) + persona_chat.estimate_tokens(user)
    assert total <= persona_chat.PROMPT_TOKEN_BUDGET


def test_service_tone_filter_still_applies_with_custom_persona(isolated_persona):
    """换人设不影响客服腔过滤：这类回复照旧整条丢弃。"""
    isolated_persona.write_text("你是学姐，禁止客服腔。\n状态：{status}\n", encoding="utf-8")
    persona_chat._system_prompt("在机房")
    assert persona_chat.clean_reply("作为AI助手，很高兴为您服务") == ""
    assert persona_chat.clean_reply("有什么可以帮您的吗") == ""


# ---------------- Prompt 预算 ----------------


def test_build_conversation_keeps_total_under_budget():
    context = [f"[群友{i}]: 这题我TLE了，线段树是不是写炸了，帮我看看第{i}行" for i in range(40)]
    system, user = persona_chat.build_conversation(
        nickname="小明", text="学姐我卡在区间DP了" * 10,
        status_text="刚点完外卖，正在B站看算法区UP主整活",
        facts=["我叫小明", "我擅长线段树", "我讨厌TLE", "我AC了100题", "我卡在DP"],
        context=context,
    )
    total = persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(user)
    assert total <= persona_chat.PROMPT_TOKEN_BUDGET
    # 上下文只保留最近若干条，不能把整个滑窗都塞进去
    assert user.count("[群友") <= persona_chat.CONTEXT_WINDOW
    assert "学姐" in system


def test_build_conversation_survives_extreme_text():
    system, user = persona_chat.build_conversation(
        nickname="n" * 60, text="卡" * 2000,
        status_text="状" * 400, facts=["标签" * 20] * 10, context=["[a]: b"] * 30,
    )
    total = persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(user)
    assert total <= persona_chat.PROMPT_TOKEN_BUDGET


# ---------------- 每日生活状态机 ----------------


def test_daily_state_slots_are_three_fixed_times():
    assert persona_chat.DAILY_STATE_SLOTS == ((12, 30), (18, 30), (23, 0))


def test_get_daily_state_initializes_default_when_missing(memory_db):
    state = db.get_daily_state()
    assert state["status_text"] == db.DEFAULT_DAILY_STATUS
    # 读取即初始化，再读一次仍是同一条（不会重复插入多行）
    assert db.get_daily_state()["status_text"] == db.DEFAULT_DAILY_STATUS


def test_daily_state_job_rotates_from_pool(memory_db):
    db.set_daily_state(db.DEFAULT_DAILY_STATUS)
    for _ in range(5):
        status = asyncio.run(persona_chat.daily_state_job())
        assert status in persona_chat.DAILY_STATUS_POOL
        assert db.get_daily_state()["status_text"] == status


def test_rotate_daily_state_persist_failure_does_not_raise():
    def boom(_value):
        raise RuntimeError("db down")

    status = persona_chat.rotate_daily_state(rng=lambda: 0.0, persist=boom)
    assert status == persona_chat.DAILY_STATUS_POOL[0]


def test_pick_daily_status_covers_pool_edges():
    pool = persona_chat.DAILY_STATUS_POOL
    assert persona_chat.pick_daily_status(rng=lambda: 0.0) == pool[0]
    assert persona_chat.pick_daily_status(rng=lambda: 0.999999) == pool[-1]


# ---------------- 表情包标签 ----------------


@pytest.mark.parametrize("raw,expected_text,expected_category", [
    ("这题我也不会 [MEME:ac]", "这题我也不会", "ac"),
    ("又TLE了[MEME:WA_TLE]", "又TLE了", "wa_tle"),
    ("我看看【占位】 [meme：question]", "我看看【占位】", "question"),
    ("摸鱼中 [MEME:slacking] ", "摸鱼中", "slacking"),
    ("别吵 [MEME:interrupt]", "别吵", "interrupt"),
    # 非法目录：标签照样剥掉，但不带分类（避免拼出不存在的素材路径）
    ("哈哈 [MEME:unknown]", "哈哈", ""),
    ("没有标签的一句话", "没有标签的一句话", ""),
])
def test_split_meme_tag(raw, expected_text, expected_category):
    text, category = persona_chat.split_meme_tag(raw)
    assert text == expected_text
    assert category == expected_category
    assert "[MEME" not in text.upper()


# ---------------- 0 Token 记忆提取与长期记忆 ----------------


@pytest.mark.parametrize("text,expected", [
    ("我叫刘灏锃，是集训队的", "我叫刘灏锃"),
    ("我是大二的", "我是大二的"),
    ("我擅长线段树", "我擅长线段树"),
    ("我卡在区间DP了", "我卡在区间DP了"),
    ("我AC了这题", "我AC了这题"),
    ("我退役了", "我退役了"),
    ("今天天气不错", ""),
])
def test_extract_memory_fact(text, expected):
    assert persona_chat.extract_memory_fact(text) == expected


def test_extract_memory_fact_truncates_long_fact():
    fact = persona_chat.extract_memory_fact("我叫" + "很" * 30)
    assert len(fact) <= db.MEMORY_FACT_MAX_CHARS


def test_memory_facts_keep_latest_five(memory_db):
    for index in range(7):
        assert db.add_memory_fact("g1", "u1", f"标签{index}")
    facts = db.list_memory_facts("g1", "u1", limit=10)
    assert len(facts) == db.MEMORY_FACTS_KEEP
    # 淘汰最旧：0、1 被挤掉，最新一条在最前
    assert facts[0] == "标签6"
    assert "标签0" not in facts
    assert "标签1" not in facts


def test_memory_facts_isolated_per_user_and_group(memory_db):
    db.add_memory_fact("g1", "u1", "我叫甲")
    db.add_memory_fact("g1", "u2", "我叫乙")
    db.add_memory_fact("g2", "u1", "我叫丙")
    assert db.list_memory_facts("g1", "u1") == ["我叫甲"]
    assert db.list_memory_facts("g1", "u2") == ["我叫乙"]
    assert db.list_memory_facts("g2", "u1") == ["我叫丙"]
    assert db.list_memory_facts("g1", "u9") == []


def test_memory_fact_limit_returns_recent_only(memory_db):
    for index in range(5):
        db.add_memory_fact("g1", "u1", f"标签{index}")
    assert db.list_memory_facts("g1", "u1", limit=2) == ["标签4", "标签3"]


def test_memory_fact_rejects_overlong_and_empty(memory_db):
    assert db.add_memory_fact("g1", "u1", "字" * (db.MEMORY_FACT_MAX_CHARS + 1)) is False
    assert db.add_memory_fact("g1", "u1", "   ") is False
    assert db.list_memory_facts("g1", "u1") == []


def test_memory_fact_recall_feeds_prompt(memory_db):
    db.add_memory_fact("g1", "u1", "我叫小明")
    db.add_memory_fact("g1", "u1", "我擅长线段树")
    facts = db.list_memory_facts("g1", "u1", limit=persona_chat.MEMORY_FACTS_INJECT)
    _system, user = persona_chat.build_conversation(
        nickname="小明", text="学姐今天干嘛", status_text="在机房摸鱼中",
        facts=facts, context=[],
    )
    assert "我叫小明" in user and "我擅长线段树" in user


# ---------------- 高潮插话闸门 ----------------


def _gate(**kwargs):
    clock = {"now": 1000.0}
    kwargs.setdefault("clock", lambda: clock["now"])
    kwargs.setdefault("rng", lambda: 0.0)
    return persona_chat.InterjectionGate(**kwargs), clock


def test_gate_requires_min_messages():
    gate, clock = _gate()
    assert all(not gate.observe("g1") for _ in range(persona_chat.INTERJECT_MIN_MESSAGES - 1))
    assert gate.observe("g1") is True


def test_gate_cooldown_blocks_second_interjection():
    gate, clock = _gate()
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES):
        gate.observe("g1")
    # 冷却未满 60 分钟：即便条数够、概率命中也不说话
    clock["now"] += persona_chat.INTERJECT_COOLDOWN_SECONDS - 1
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES + 5):
        assert gate.observe("g1") is False


def test_gate_allows_after_cooldown_expires():
    gate, clock = _gate()
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES):
        gate.observe("g1")
    clock["now"] += persona_chat.INTERJECT_COOLDOWN_SECONDS + 1
    results = [gate.observe("g1") for _ in range(persona_chat.INTERJECT_MIN_MESSAGES)]
    assert any(results)


def test_gate_probability_miss_keeps_quiet():
    gate, _clock = _gate(rng=lambda: 0.99)
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES * 3):
        assert gate.observe("g1") is False


def test_gate_window_expires_old_messages():
    gate, clock = _gate()
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES):
        gate.observe("g1")
    # 距上一条已超出 2 分钟窗口：窗口内只剩当前这条，不够 12 条
    clock["now"] += persona_chat.INTERJECT_WINDOW_SECONDS + 1
    assert gate.observe("g1") is False


def test_gate_reset_clears_state():
    gate, clock = _gate()
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES):
        gate.observe("g1")
    gate.reset("g1")
    for _ in range(persona_chat.INTERJECT_MIN_MESSAGES - 1):
        assert gate.observe("g1") is False


def test_singletons_are_stable_and_resettable():
    persona_chat.reset_engine()
    assert persona_chat.get_memory() is persona_chat.get_memory()
    assert persona_chat.get_gate() is persona_chat.get_gate()
    persona_chat.reset_engine()
    assert persona_chat.get_memory() is not None


# ---------------- 滑窗与回复清洗 ----------------


def test_memory_window_keeps_recent_six_and_isolated():
    memory = persona_chat.PersonaMemory()
    for index in range(10):
        memory.observe("g1", f"群友{index}", f"消息{index}")
    memory.observe("g2", "别人", "另一个群")
    recent = memory.recent("g1")
    assert len(recent) == persona_chat.CONTEXT_WINDOW
    assert recent[-1] == "[群友9]: 消息9"
    assert memory.recent("g2") == ["[别人]: 另一个群"]
    memory.reset("g1")
    assert memory.recent("g1") == []


def test_clean_reply_drops_service_tone():
    assert persona_chat.clean_reply("作为AI助手，很高兴为您服务") == ""
    assert persona_chat.clean_reply("有什么可以帮您的吗") == ""
    assert persona_chat.clean_reply("   ") == ""


def test_clean_reply_strips_prefix_and_truncates():
    assert persona_chat.clean_reply("学姐：这题我也不会") == "这题我也不会"
    assert len(persona_chat.clean_reply("哈" * 300)) == persona_chat.REPLY_MAX_CHARS


# ---------------- 与现有指令的边界 ----------------


def test_prompt_static_persona_mentions_jargon_and_hides_customer_tone():
    system, _user = persona_chat.build_conversation(
        nickname="小明", text="在吗", status_text="在机房摸鱼中",
    )
    assert "学姐" in system
    for word in ("WA", "TLE", "AK", "DP", "线段树"):
        assert word in system
    assert "1~2 句" in system
    assert "作为AI助手" not in system.replace("不说「作为AI助手", "")
    assert "MEME" in system


def test_state_updated_at_is_iso_like(memory_db):
    db.set_daily_state("正在整理今晚的补题清单", now=datetime(2026, 11, 15, 12, 30, 0))
    state = db.get_daily_state()
    assert state["status_text"] == "正在整理今晚的补题清单"
    assert state["updated_at"].startswith("2026-11-15 12:30")
