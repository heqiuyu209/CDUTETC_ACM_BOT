"""入库前 LLM 智能提炼：阈值直返、精简、超时/异常降级。"""

import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database, llm
from cdutetc.config import TZ_NAME

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=ZoneInfo(TZ_NAME))

SHORT = "带电脑"            # 3 字
EDGE = "算" * llm.ENTRY_SUMMARY_THRESHOLD      # 恰好 35 字
LONG = "算" * (llm.ENTRY_SUMMARY_THRESHOLD + 1)  # 36 字，越阈值


@pytest.fixture
def calls(monkeypatch):
    """记录 _summarize_entry 是否被调用。"""
    box = {"count": 0}

    async def fake(category, title, raw_text, timeout=llm.ENTRY_SUMMARY_TIMEOUT):
        box["count"] += 1
        box["category"] = category
        box["title"] = title
        return "精简后的要求"

    monkeypatch.setattr(llm, "_summarize_entry", fake)
    return box


@pytest.fixture
def env(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(commands, "_now_local", lambda: NOW)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or NOW)
    database.init_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------- 阈值与直返

@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_blank_text_returns_empty_without_llm(calls, raw):
    assert llm.summarize_entry_with_llm("课程", "算法集训", raw) == ("", False)
    assert calls["count"] == 0


def test_short_text_returned_as_is_without_llm(calls):
    assert llm.summarize_entry_with_llm("课程", "算法集训", "  带电脑  ") == ("带电脑", False)
    assert calls["count"] == 0


def test_text_at_threshold_not_summarized(calls):
    result, summarized = llm.summarize_entry_with_llm("课程", "算法集训", EDGE)
    assert summarized is False
    assert result == EDGE
    assert calls["count"] == 0


def test_text_over_threshold_calls_llm(calls):
    result, summarized = llm.summarize_entry_with_llm("课程", "算法集训", LONG)
    assert (result, summarized) == ("精简后的要求", True)
    assert calls["count"] == 1
    assert calls["category"] == "课程"
    assert calls["title"] == "算法集训"


def test_overlong_model_output_is_truncated(calls, monkeypatch):
    async def verbose(category, title, raw_text, timeout=llm.ENTRY_SUMMARY_TIMEOUT):
        return "很" * 200

    monkeypatch.setattr(llm, "_summarize_entry", verbose)
    result, summarized = llm.summarize_entry_with_llm("课程", "算法集训", LONG)
    assert summarized is True
    assert len(result) <= llm.ENTRY_SUMMARY_MAX_CHARS


# ---------------------------------------------------------------- 降级

def test_model_exception_degrades_to_raw_text(calls, monkeypatch):
    async def boom(category, title, raw_text, timeout=llm.ENTRY_SUMMARY_TIMEOUT):
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(llm, "_summarize_entry", boom)
    assert llm.summarize_entry_with_llm("课程", "算法集训", LONG) == (LONG, False)


def test_model_timeout_degrades_to_raw_text(calls, monkeypatch):
    async def slow(category, title, raw_text, timeout=llm.ENTRY_SUMMARY_TIMEOUT):
        await asyncio.sleep(0.5)
        return "不该拿到的摘要"

    monkeypatch.setattr(llm, "_summarize_entry", slow)
    result, summarized = llm.summarize_entry_with_llm("课程", "算法集训", LONG, timeout=0.05)
    assert (result, summarized) == (LONG, False)


def test_empty_model_output_degrades_to_raw_text(calls, monkeypatch):
    async def blank(category, title, raw_text, timeout=llm.ENTRY_SUMMARY_TIMEOUT):
        return "   "

    monkeypatch.setattr(llm, "_summarize_entry", blank)
    assert llm.summarize_entry_with_llm("课程", "算法集训", LONG) == (LONG, False)


# ---------------------------------------------------------------- 统一入口

def test_prepare_requirement_swallows_llm_exception(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("提炼服务挂了")

    monkeypatch.setattr(llm, "summarize_entry_with_llm", boom)
    assert commands._prepare_requirement(LONG, "算法集训") == (LONG, False)


def test_prepare_requirement_blank_short_circuits(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("空文本不应调用提炼")

    monkeypatch.setattr(llm, "summarize_entry_with_llm", boom)
    assert commands._prepare_requirement("  ", "算法集训") == ("", False)


# ---------------------------------------------------------------- 命令集成

def test_training_add_marks_summarized_requirement(env, monkeypatch):
    monkeypatch.setattr(commands, "_prepare_requirement",
                        lambda raw, title, category="课程": ("带电脑，提前装好环境", True))
    text = commands.cmd_training_add(
        "g1", "teacher", "标题=算法集训 时间=周四 19:00-21:00 要求=这段要求非常长" * 1,
    )[0]["text"]

    assert "（已智能提炼）" in text
    row = database.list_training_sessions("g1")[0]
    assert row["requirement"] == "带电脑，提前装好环境"
    assert row["title"] == "算法集训"


def test_reminder_add_marks_summarized_content(env, monkeypatch):
    monkeypatch.setattr(commands, "_prepare_requirement",
                        lambda raw, title, category="提醒": ("交实验报告", True))
    text = commands.cmd_reminder_add("g1", "teacher", "2026-11-20 18:00 这段提醒内容非常长")[0]["text"]

    assert "已自动提炼摘要" in text
    assert "下次提醒：2026-11-20 18:00" in text
    assert database.list_custom_reminders("g1")[0]["content"] == "交实验报告"


def test_reminder_add_plain_content_has_no_summary_hint(env, monkeypatch):
    monkeypatch.setattr(commands, "_prepare_requirement",
                        lambda raw, title, category="提醒": ("交作业", False))
    text = commands.cmd_reminder_add("g1", "teacher", "2026-11-20 18:00 交作业")[0]["text"]

    assert "已自动提炼摘要" not in text
    assert "下次提醒：2026-11-20 18:00" in text


def test_reminder_modify_marks_summarized_content(env, monkeypatch):
    database.create_custom_reminder("g1", "交作业", "2026-11-14 18:00",
                                    repeat_rule="none", weekday="", time_of_day="18:00",
                                    operator="teacher")
    monkeypatch.setattr(commands, "_prepare_requirement",
                        lambda raw, title, category="提醒": ("交实验报告", True))
    text = commands.cmd_reminder_modify("g1", "teacher", "1 这段新内容非常长")[0]["text"]

    assert "已自动提炼摘要" in text
    assert database.list_custom_reminders("g1")[0]["content"] == "交实验报告"
