"""真实命令链与隔离 SQLite：确认、共享比赛归档恢复及规则持久化。"""

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, commands, config, database as db, group_cleanup


@pytest.fixture
def env(monkeypatch, tmp_path):
    path = tmp_path / "cleanup_command_integration.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or datetime(
        2026, 11, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "other"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "other"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    db.init_db()
    admin_session.reset_all()
    yield path
    admin_session.reset_all()


async def test_private_archive_confirmation_and_restore_preserve_shared_record(env):
    cid = db.create_competition({"name": "正式旧比赛", "official_time": "2026-09-01 19:00"}, status="confirmed")
    admin_session.set_active_group("admin", "team")
    preview = await commands.handle_command("/清理预览 过期比赛", "other", "admin",
                                            private=True, active_group="other")
    assert f"比赛 #{cid}：正式旧比赛" in preview[0]["text"]
    token = re.search(r"/清理确认 (\S+)", preview[0]["text"]).group(1)
    result = await commands.handle_command(f"/清理确认 {token}", "other", "admin", private=True)
    assert "比赛 1 项" in result[0]["text"]
    assert "备份已保存" in result[0]["text"]
    with closing(sqlite3.connect(env)) as conn:
        assert conn.execute("SELECT group_id,competition_id FROM group_competition_archives").fetchall() == [("team", cid)]
        assert conn.execute("SELECT id FROM competitions").fetchall() == [(cid,)]
    result = await commands.handle_command(f"/恢复比赛 {cid}", "", "admin", private=True)
    assert "已恢复" in result[0]["text"]
    with closing(sqlite3.connect(env)) as conn:
        assert conn.execute("SELECT * FROM group_competition_archives").fetchall() == []


async def test_test_question_mark_preview_cleanup_and_unmark(env):
    qid = db.add_unanswered_question("team", "调试问题", "member")
    await commands.handle_command(f"/标记测试 问题 {qid}", "team", "admin")
    preview = await commands.handle_command("/清理预览 测试数据", "team", "admin")
    assert "调试问题" in preview[0]["text"]
    await commands.handle_command(f"/取消测试标记 问题 {qid}", "team", "admin")
    assert group_cleanup.preview_cleanup("team", "test_data", days=0, operator="admin")["items"] == []
    await commands.handle_command(f"/标记测试 问题 {qid}", "team", "admin")
    preview = await commands.handle_command("/清理预览 测试数据", "team", "admin")
    token = re.search(r"/清理确认 (\S+)", preview[0]["text"]).group(1)
    result = await commands.handle_command(f"/清理确认 {token}", "team", "admin")
    assert "问题 1 项" in result[0]["text"]
    with closing(sqlite3.connect(env)) as conn:
        assert conn.execute("SELECT id FROM unanswered_questions WHERE id=?", (qid,)).fetchone() is None


async def test_selected_group_rule_persists_without_other_group_effect(env):
    admin_session.set_active_group("admin", "other")
    result = await commands.handle_command("/清理规则 测试数据 2天 开启", "team", "admin", private=True)
    assert "测试数据：开启，保留 2 天" in result[0]["text"]
    assert group_cleanup.get_rules("other")["test_data"]["enabled"] is True
    assert group_cleanup.get_rules("team")["test_data"]["enabled"] is False
