"""工作区内老师 QQ/标识与可选姓名：真实别名映射、授权和群隔离。"""

import sqlite3
from contextlib import closing

import pytest

from cdutetc import admin_session, commands, config, group_admin
from cdutetc import database as db

QQ = "247871156"
TEACHER = "teacher-real-openid"
OTHER = "other-real-openid"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "teacher-binding.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {TEACHER, OTHER})
    admin_session.reset_all()
    db.init_db()
    db.set_account_label("group", "team", "集训队")
    db.set_account_label("group", "new", "新生群")
    db.set_entity_alias("user", TEACHER, "张老师", QQ)
    db.set_entity_alias("user", OTHER, "李老师", "246802468")
    admin_session.set_active_group("admin", "team")
    yield
    admin_session.reset_all()


def _bind(args, sender="admin", current_group="", private=True):
    return group_admin.handle_command("群老师", args, sender, current_group, private=private)[0]["text"]


@pytest.mark.parametrize("args", [QQ, f"{QQ} 张老师", TEACHER, f"{TEACHER} 张老师", "张老师"])
async def test_short_binding_uses_current_workspace_and_real_authorized_openid(env, args):
    before = set(config.TEACHER_WHITELIST)
    reply = await commands.handle_command(f"/绑老师 {args}", "new", "admin", private=True)
    assert "集训队负责老师" in reply[0]["text"] and "张老师" in reply[0]["text"]
    assert db.get_group_teachers("team") == [TEACHER]
    assert not db.get_group_teachers("new")
    assert set(config.TEACHER_WHITELIST) == before and QQ not in config.TEACHER_WHITELIST
    assert not db.list_runtime_teachers()
    assert db.get_entity_alias("user", QQ) is None


@pytest.mark.parametrize("selector", ["new", "新生", "1", "123456789"])
def test_explicit_group_is_preserved_with_teacher_qq_and_optional_name(env, selector):
    db.set_entity_alias("group", "new", "新生群", "123456789")
    assert "新生群负责老师" in _bind(f"{selector} {QQ} 张老师")
    assert db.get_group_teachers("new") == [TEACHER]
    assert not db.get_group_teachers("team")


@pytest.mark.parametrize("args", [f"{TEACHER},{OTHER}", "张老师，李老师", f"{TEACHER} {OTHER}", f"{QQ} 246802468"])
def test_existing_multiple_teacher_syntax_still_binds_both(env, args):
    assert "负责老师" in _bind(args)
    assert set(db.get_group_teachers("team")) == {TEACHER, OTHER}


@pytest.mark.parametrize("args", [f"{QQ} 新姓名", f"{TEACHER} 新姓名"])
def test_optional_name_updates_same_identity_and_keeps_numeric_mapping(env, args):
    assert "新姓名" in _bind(args)
    assert db.get_group_teachers("team") == [TEACHER]
    alias = db.get_entity_alias("user", TEACHER)
    assert alias["numeric_id"] == QQ and alias["display_name"] == "新姓名"
    assert db.get_account_label("user", TEACHER) == "新姓名"
    assert not db.list_runtime_teachers()


def test_openid_and_name_does_not_invent_numeric_alias(env):
    db.remove_entity_alias("user", TEACHER)
    db.remove_account_label("user", TEACHER)
    assert "张老师" in _bind(f"{TEACHER} 张老师")
    assert db.get_account_label("user", TEACHER) == "张老师"
    assert db.get_entity_alias("user", TEACHER) is None
    assert db.get_group_teachers("team") == [TEACHER]


def test_unclaimed_qq_gives_real_identity_guidance_and_changes_nothing(env):
    db.set_group_teachers("team", {OTHER}, operator="admin")
    text = _bind("999888777 王老师")
    assert "QQ 999888777 尚未认领" in text and "/老师列表" in text and "/绑老师 <老师标识> <姓名>" in text
    assert "/加老师 <openid> <QQ号> <姓名>" in text and "没有匹配到唯一" not in text
    assert db.get_group_teachers("team") == [OTHER]
    assert "999888777" not in config.TEACHER_WHITELIST
    assert not db.list_runtime_teachers()


def test_raw_numeric_whitelist_entry_is_not_used_as_official_openid(env, monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {QQ})
    db.remove_entity_alias("user", TEACHER)
    assert "尚未认领" in _bind(f"{QQ} 张老师")
    assert not db.get_group_teachers("team")


def test_qq_mapping_does_not_authorize_unlisted_teacher(env, monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {OTHER})
    text = _bind(f"{QQ} 张老师")
    assert "尚未授权" in text and "真实老师标识 /加老师" in text
    assert not db.get_group_teachers("team") and not db.list_runtime_teachers()


def test_duplicate_qq_mapping_is_rejected_even_if_one_identity_is_unauthorized(env):
    db.set_entity_alias("user", "unlisted", "同名老师", QQ)
    assert "多个老师标识" in _bind(f"{QQ} 张老师")
    assert not db.get_group_teachers("team")


def test_duplicate_teacher_names_keep_legacy_ambiguity_rejection(env):
    db.set_entity_alias("user", OTHER, "张老师", "246802468")
    assert "歧义" in _bind("张老师")
    assert "歧义" in _bind(f"{TEACHER} 张老师")
    assert not db.get_group_teachers("team")


def test_group_and_teacher_numeric_collision_requires_explicit_group(env):
    db.set_entity_alias("group", "new", "新生群", QQ)
    assert "同时对应群和老师 QQ" in _bind(f"{QQ} 张老师")
    assert not db.get_group_teachers("team") and not db.get_group_teachers("new")
    assert "集训队负责老师" in _bind(f"team {QQ} 张老师")
    assert db.get_group_teachers("team") == [TEACHER]


@pytest.mark.parametrize("sender,private", [(TEACHER, True), (TEACHER, False), ("member", False)])
def test_binding_requires_superadmin_in_private_or_group(env, sender, private):
    _bind(f"{QQ} 新姓名", sender=sender, current_group="team", private=private)
    assert not db.get_group_teachers("team")
    assert db.get_entity_alias("user", TEACHER)["display_name"] == "张老师"


def test_group_command_is_locked_to_this_group(env):
    assert "跨群" in _bind(f"new {QQ} 新姓名", current_group="team", private=False)
    assert not db.get_group_teachers("new")
    assert db.get_entity_alias("user", TEACHER)["display_name"] == "张老师"
    assert "集训队负责老师" in _bind(f"{QQ} 张老师", current_group="team", private=False)
    assert db.get_group_teachers("team") == [TEACHER]


def test_binding_without_workspace_requires_explicit_group(env):
    admin_session.clear_active_group("admin")
    assert "先 /切换" in _bind(f"{QQ} 张老师")
    assert not db.get_group_teachers("team")
    assert "新生群负责老师" in _bind(f"new {QQ} 张老师")


def test_clear_and_query_keep_existing_behaviour(env):
    _bind(QQ)
    assert "张老师" in _bind("")
    assert "未登记" in _bind("清空")
    assert not db.get_group_teachers("team")


def test_name_and_group_assignment_roll_back_together_if_assignment_fails(env):
    db.set_group_teachers("team", {OTHER}, operator="admin")
    with closing(db._connect()) as conn, conn:
        conn.execute("CREATE TRIGGER reject_teacher BEFORE INSERT ON group_teacher_assignments"
                     " BEGIN SELECT RAISE(ABORT, 'offline binding failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="offline binding failure"):
        _bind(f"{QQ} 新姓名")
    assert db.get_group_teachers("team") == [OTHER]
    assert db.get_account_label("user", TEACHER) == "张老师"
    assert db.get_entity_alias("user", TEACHER)["display_name"] == "张老师"


def test_unknown_second_numeric_identity_does_not_become_teacher_name(env):
    assert "尚未认领" in _bind(f"{QQ} 999888777")
    assert not db.get_group_teachers("team")
    assert db.get_entity_alias("user", TEACHER)["display_name"] == "张老师"
