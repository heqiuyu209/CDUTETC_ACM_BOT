"""群归档在老师自然语言、重复发布及通知群展示中的真实数据库回归。"""

import sqlite3
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, group_cleanup, main, publish_format, teacher_intent
from cdutetc.handlers import admin_cmd

NOW = datetime(2026, 11, 18, 10, 0)
TARGET = "2026-11-20 19:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "integration.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", False)
    db.init_db()
    return path


def _contest(name="校赛", groups=None):
    cid = db.create_competition({"name": name, "official_time": TARGET}, status="confirmed")
    if groups is not None:
        db.set_competition_notify_groups(cid, groups)
    return cid


def _rows(path, query, args=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(query, args).fetchall()


def test_teacher_context_excludes_foreign_and_archived_contests(env):
    own = _contest("本群场次", {"g1"})
    foreign = _contest("另群场次", {"g2"})
    archived = _contest("归档场次")
    assert group_cleanup.archive_competition("g1", archived, operator="admin")
    _, contests = teacher_intent.build_context("g1", NOW)
    assert {item["id"] for item in contests} == {own}
    _, others = teacher_intent.build_context("g2", NOW)
    assert {item["id"] for item in others} == {foreign, archived}


@pytest.mark.parametrize("archived,cancel", [(False, False), (False, True), (True, False), (True, True)])
async def test_model_selected_foreign_or_archived_contest_is_rechecked(env, monkeypatch, archived, cancel):
    cid = _contest(groups=None if archived else {"g2"})
    if archived:
        group_cleanup.archive_competition("g1", cid, operator="admin")
    before = db.get_competition(cid)
    scope = db.get_competition_notify_groups(cid)
    deliveries = _rows(env, "SELECT * FROM reminder_deliveries")
    monkeypatch.setattr(teacher_intent, "classify", AsyncMock(return_value={
        "intent": "UPDATE_CANCEL", "confidence": "high", "target_id": cid,
        "cancel": cancel, "date": "2026-11-21", "start_time": "19:00",
    }))
    result = await teacher_intent.handle_message("明天校赛取消或者改期", group_id="g1", sender_id="teacher", now=NOW)
    assert result["action"] == "pending"
    assert db.get_competition(cid) == before
    assert db.get_competition_notify_groups(cid) == scope
    assert _rows(env, "SELECT * FROM reminder_deliveries") == deliveries


async def test_teacher_cancellation_archives_only_this_group(env, monkeypatch):
    cid = _contest()
    before = db.get_competition(cid)
    others = _rows(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'")
    monkeypatch.setattr(teacher_intent, "classify", AsyncMock(return_value={
        "intent": "UPDATE_CANCEL", "confidence": "high", "target_id": cid, "cancel": True,
    }))
    result = await teacher_intent.handle_message("明天校赛取消", group_id="g1", sender_id="teacher", now=NOW)
    assert result["action"] == "saved" and "已取消比赛推送" in result["reply"]
    assert db.get_competition(cid) == before
    assert db.get_competition_notify_groups(cid) == set()
    assert db.get_competition_target_groups(cid) == {"g2"}
    assert _rows(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'") == others
    db.sync_future_contest_start_reminders()
    assert not _rows(env, "SELECT 1 FROM reminder_deliveries WHERE group_id='g1' AND status='pending'")


async def test_teacher_cancellation_preserves_inflight_lease(env, monkeypatch):
    cid = _contest()
    with closing(sqlite3.connect(env)) as conn:
        conn.execute("UPDATE reminder_deliveries SET status='sending',lease_token='lease',lease_until='2026-11-20 18:35' WHERE group_id='g1'")
        conn.commit()
    before = _rows(env, "SELECT * FROM reminder_deliveries")
    monkeypatch.setattr(teacher_intent, "classify", AsyncMock(return_value={
        "intent": "UPDATE_CANCEL", "confidence": "high", "target_id": cid, "cancel": True,
    }))
    result = await teacher_intent.handle_message("明天校赛取消", group_id="g1", sender_id="teacher", now=NOW)
    assert result["action"] == "pending"
    assert db.get_competition_target_groups(cid) == {"g1", "g2"}
    assert _rows(env, "SELECT * FROM reminder_deliveries") == before


def test_teacher_amendment_keeps_other_groups_subscribed(env):
    cid = _contest()
    applied = teacher_intent._apply_update("g1", {"target_id": cid, "date": "2026-11-21", "start_time": "19:00"}, NOW, "teacher")
    assert applied[0] == "contest_updated"
    assert db.get_competition_target_groups(cid) == {"g1", "g2"}
    assert db.get_competition_notify_groups(cid) == set()
    assert db.list_deliveries("g2", statuses=("pending",))


def test_teacher_update_rejects_draft_target(env):
    cid = db.create_competition({"name": "尚未确认", "official_time": TARGET})
    original = db.get_competition(cid)
    assert teacher_intent._apply_update("g1", {"target_id": cid, "date": "2026-11-21", "start_time": "19:00"}, NOW, "teacher") is None
    assert db.get_competition(cid) == original


@pytest.mark.parametrize("scope", [None, {"g1", "g2"}])
async def test_duplicate_publication_keeps_archive_and_explains_restore(env, monkeypatch, official_client, scope):
    cid = _contest(groups=scope)
    group_cleanup.archive_competition("g1", cid, operator="admin")
    before = db.get_competition(cid)
    bot = main.ACMBot(official_client)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "_safe_reply", reply)
    monkeypatch.setattr(main, "parse_simple_publication", lambda *a, **kw: {"name": "校赛", "contest_time": TARGET})
    await bot._handle_publish(SimpleNamespace(), "校赛 2026-11-20 19:00", "g1", "teacher")
    text = reply.await_args.args[1]
    assert "本群已归档" in text and f"/恢复比赛 {cid}" in text
    assert len(db.list_competitions()) == 1
    assert db.get_competition(cid) == before
    assert db.get_competition_target_groups(cid) == {"g2"}
    official_client.api.post_group_message.assert_not_awaited()


def test_duplicate_scope_extension_does_not_readd_archived_group(env):
    cid = _contest()
    group_cleanup.archive_competition("g1", cid, operator="admin")
    db.set_competition_notify_groups(cid, {"g2"})
    assert not publish_format._extend_scope_with_group(cid, "g1")
    assert db.get_competition_notify_groups(cid) == {"g2"}
    assert db.get_competition_target_groups(cid) == {"g2"}


async def test_duplicate_approval_receipt_preserves_source_archive(env, monkeypatch, official_client):
    cid = _contest()
    group_cleanup.archive_competition("g1", cid, operator="admin")
    pid = db.create_pending_publication({"name": "校赛", "official_time": TARGET}, "g1", "", "", "teacher")
    bot = main.ACMBot(official_client)
    send_private = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "_send_private_result", send_private)
    assert await bot._handle_publish_decision("admin", f"同意 {pid}")
    text = send_private.await_args.args[1]
    assert "来源群已归档" in text and f"/恢复比赛 {cid}" in text
    assert db.get_competition_target_groups(cid) == {"g2"}
    official_client.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("args", ["", " 全部"])
def test_notify_scope_reports_actual_groups_and_keeps_archive(env, args):
    cid = _contest()
    group_cleanup.archive_competition("g1", cid, operator="admin")
    text = admin_cmd.cmd_notify_groups("g1", "admin", str(cid) + args)[0]["text"]
    assert "实际目标：g2" in text and "本群已归档" in text and f"/恢复比赛 {cid}" in text
    assert db.get_competition_target_groups(cid) == {"g2"}
