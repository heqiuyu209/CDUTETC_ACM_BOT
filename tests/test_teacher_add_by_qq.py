"""覆盖 /加老师 三段式（openid QQ号 姓名）授权、备注落库与防冒充。

三段式中 openid 进白名单授权，QQ号写入 entity_aliases.numeric_id、姓名作为
display_name 与备注落库，QQ号绝不进白名单。仅给 openid 的兼容路径保留。
认领老师命令删除后，纯 QQ 号反查已移除，第一段为纯数字时应被拒绝。
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


# ------------------------------------------------ 三段式：openid QQ号 姓名
def test_add_teacher_three_part_writes_alias_and_whitelist(memory_db):
    _add(f"{OPENID} 10001 谢")
    assert OPENID in config.TEACHER_WHITELIST
    assert "10001" not in config.TEACHER_WHITELIST, "QQ 号原样入白名单会造成身份失配"
    alias = database.get_entity_alias("user", OPENID)
    assert alias["numeric_id"] == "10001"
    assert alias["display_name"] == "谢"
    rows = database.list_runtime_teachers()
    assert [r["user_id"] for r in rows] == [OPENID]
    # 姓名作为备注落库
    assert rows[0]["note"] == "谢"


def test_add_teacher_three_part_note_uses_name(memory_db):
    _add(f"{OPENID} 10001 张老师")
    alias = database.get_entity_alias("user", OPENID)
    assert alias["display_name"] == "张老师"
    assert database.list_runtime_teachers()[0]["note"] == "张老师"


def test_add_teacher_three_part_updates_existing_alias(memory_db):
    database.set_entity_alias("user", OPENID, "旧名", "99999")
    _add(f"{OPENID} 10001 王老师")
    alias = database.get_entity_alias("user", OPENID)
    assert alias["numeric_id"] == "10001"
    assert alias["display_name"] == "王老师"


# ------------------------------------------------ 纯 QQ 号反查已移除
def test_add_teacher_bare_qq_is_rejected(memory_db):
    result = _add("10001 负责训练")
    assert "不能用" in result["text"] or "QQ 号不能直接" in result["text"]
    assert "/加老师 <openid> <QQ号> <姓名>" in result["text"]
    assert result.get("notify_super_admin") is False
    assert not database.list_runtime_teachers()
    assert "10001" not in config.TEACHER_WHITELIST


def test_add_teacher_three_part_requires_name(memory_db):
    result = _add(f"{OPENID} 10001")
    assert "姓名不能为空" in result["text"]
    assert not database.list_runtime_teachers()


# ------------------------------------------------ openid 兼容路径
def test_add_teacher_by_openid_keeps_legacy_path(memory_db):
    result = _add(f"{OPENID} 负责训练")
    assert "已添加老师" in result["text"]
    assert OPENID in config.TEACHER_WHITELIST
    assert [r["user_id"] for r in database.list_runtime_teachers()] == [OPENID]


def test_add_teacher_by_openid_only(memory_db):
    result = _add(OPENID)
    assert "已添加老师" in result["text"]
    assert OPENID in config.TEACHER_WHITELIST
    assert [r["user_id"] for r in database.list_runtime_teachers()] == [OPENID]


# ------------------------------------------------ 权限与防冒充
def test_add_teacher_requires_super_admin(memory_db):
    result = _add(f"{OPENID} 10001 张老师", sender="member")
    assert "仅超管" in result["text"]
    assert not database.list_runtime_teachers()
