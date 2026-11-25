"""覆盖 /加老师 按 QQ 号反查 openid 授权、openid 兼容路径与防冒充。

背景：官方协议事件只带 member_openid，QQ 号不出现在事件里，无法从
“发言记录”解析 openid。QQ→openid 的唯一来源是老师本人 /认领老师 报号
写入的 entity_aliases。本文件验证超管 /加老师 <QQ号> 走映射反查授权，
且不会把 QQ 号原样写入白名单造成身份失配。
"""

import asyncio
import sqlite3

import pytest

from cdutetc import commands, config, database

GROUP = "A0EF5D2D8BA4B3CC76E3C70C47971FDE"
OPENID = "O0P1E2N3I4D5"


@pytest.fixture()
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    database.init_db()
    yield conn
    conn.close()


def _add(text, sender="admin"):
    return asyncio.run(commands.handle_command(f"/加老师 {text}", GROUP, sender))[0]


# ------------------------------------------------ 按 QQ 号反查 openid 授权
def test_add_teacher_by_qq_resolves_openid_not_qq(memory_db):
    database.set_entity_alias("user", OPENID, "张老师", "10001")
    result = _add("10001 负责训练")
    assert "解析到老师" in result["text"] and OPENID in result["text"]
    assert OPENID in config.TEACHER_WHITELIST
    assert "10001" not in config.TEACHER_WHITELIST, "QQ 号原样入白名单会造成身份失配"
    rows = database.list_runtime_teachers()
    assert [r["user_id"] for r in rows] == [OPENID]


def test_add_teacher_by_qq_uses_claim_name_as_note_when_note_omitted(memory_db):
    database.set_entity_alias("user", OPENID, "张老师", "10001")
    _add("10001")
    rows = database.list_runtime_teachers()
    assert rows[0]["user_id"] == OPENID
    assert rows[0]["note"] == "张老师"


def test_add_teacher_by_qq_without_claim_is_rejected(memory_db):
    result = _add("10001 负责训练")
    assert "尚无老师报号" in result["text"]
    assert "/认领老师" in result["text"]
    assert result.get("notify_super_admin") is False
    assert not database.list_runtime_teachers()
    assert "10001" not in config.TEACHER_WHITELIST


def test_add_teacher_by_qq_with_ambiguous_mapping_is_rejected(memory_db):
    database.set_entity_alias("user", OPENID, "张老师", "10001")
    database.set_entity_alias("user", "ANOTHER0PENID", "另一位老师", "10001")
    result = _add("10001 负责训练")
    assert "同时关联了 2 个" in result["text"]
    assert result.get("notify_super_admin") is False
    assert not database.list_runtime_teachers()


# ------------------------------------------------ openid 兼容路径
def test_add_teacher_by_openid_keeps_legacy_path(memory_db):
    result = _add(f"{OPENID} 负责训练")
    assert "已添加老师" in result["text"]
    assert OPENID in config.TEACHER_WHITELIST
    assert [r["user_id"] for r in database.list_runtime_teachers()] == [OPENID]


# ------------------------------------------------ 权限与防冒充
def test_add_teacher_requires_super_admin(memory_db):
    result = _add("10001 负责训练", sender="member")
    assert "仅超管" in result["text"]
    assert not database.list_runtime_teachers()


def test_full_flow_claim_then_add_by_qq(memory_db):
    # 1) 老师本人在群里报号：只建立映射，不自动授权。
    claim = asyncio.run(commands.handle_command(
        "/认领老师 10001 张老师", GROUP, OPENID))[0]["text"]
    assert "已登记老师报号" in claim
    assert OPENID not in config.TEACHER_WHITELIST
    assert not database.list_runtime_teachers()
    # 2) 超管按 QQ 号授权：反查到老师本人 openid。
    add = _add("10001 负责训练")
    assert OPENID in add["text"]
    assert OPENID in config.TEACHER_WHITELIST
    assert "10001" not in config.TEACHER_WHITELIST
    assert [r["user_id"] for r in database.list_runtime_teachers()] == [OPENID]
