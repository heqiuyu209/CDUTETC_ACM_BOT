"""主动通知真实业务入口的回归；内存库、模拟时钟与发送边界，无外网。"""

import asyncio
from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, main, notifications, reminder, roles
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage
from cdutetc.errors import DeliveryCode


NOW = datetime(2026, 11, 10, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
GROUPS = [letter * 32 for letter in "ABCD"]


@pytest.fixture
def store(monkeypatch):
    conn = sqlite3.connect(":memory:")
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(db, "_connect", lambda: conn)
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or clock.now)
    monkeypatch.setattr(db, "now_str", lambda: clock.now.strftime("%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set(GROUPS))
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set(GROUPS))
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ADMIN_GROUP", GROUPS[0])
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {GROUPS[0]})
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (1,))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(reminder, "datetime", Clock)
    db.init_db()
    yield SimpleNamespace(conn=conn, clock=clock)
    conn.close()


def competition(name="通知回归", when="2026-11-10 12:00"):
    return db.create_competition({"name": name, "official_time": when}, status="confirmed")


def plans(store, cid):
    return store.conn.execute(
        "SELECT group_id, scheduled_at, status, attempts FROM reminder_deliveries"
        " WHERE obj_id=? AND remind_type='contest_start' ORDER BY group_id, scheduled_at", (cid,),
    ).fetchall()


def bot():
    api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "group-receipt"}),
        post_c2c_message=AsyncMock(return_value={"id": "private-receipt"}),
    )
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    return main.ACMBot(client), api


def test_authorize_group_populates_existing_global_but_not_scoped_or_weekly_plans(store, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GROUPS[0]})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {GROUPS[0]})
    global_id = competition()
    scoped_id = competition("仅原群")
    db.set_competition_notify_groups(scoped_id, {GROUPS[0]})
    weekly_id, _ = db.create_weekly_competition(
        "2026-11-09", {"name": "周赛", "official_time": "2026-11-15 19:00"},
    )
    commands.cmd_group_add(GROUPS[0], "admin", GROUPS[1])
    assert {p[0] for p in plans(store, global_id)} == {GROUPS[0], GROUPS[1]}
    assert len(plans(store, global_id)) == 4
    assert {p[0] for p in plans(store, scoped_id)} == {GROUPS[0]}
    assert {p[0] for p in plans(store, weekly_id)} == {GROUPS[0]}
    roles.refresh()
    assert len(plans(store, global_id)) == 4


def test_startup_backfills_legacy_after_dynamic_groups_loaded_without_late_nodes(store, monkeypatch):
    cid = competition(when="2026-11-10 10:40")
    db.set_competition_notify_groups(cid, {GROUPS[1]})
    with store.conn:
        store.conn.execute("DELETE FROM reminder_deliveries")
        db.ensure_delivery(GROUPS[1], "competition", cid, "contest_time", "2026-11-10 10:40")
    db.add_runtime_group(GROUPS[1], "admin")
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    store.clock.now = NOW + timedelta(minutes=11)  # 30m node already passed, still in old grace
    db.init_db()
    roles.refresh()
    db.reconcile_deliveries(store.clock.now, allowed_groups=config.ALLOWED_GROUPS)
    assert plans(store, cid) == [(GROUPS[1], "2026-11-10 10:25:00", "pending", 0)]
    assert store.conn.execute("SELECT status FROM reminder_deliveries WHERE remind_type='contest_time'").fetchone() == ("cancelled",)
    roles.refresh()
    assert len(plans(store, cid)) == 1


@pytest.mark.parametrize("status,attempts", [("sent", 1), ("failed", 3), ("sending", 1)])
def test_resync_preserves_delivery_history(store, status, attempts):
    cid = competition()
    with store.conn:
        store.conn.execute("UPDATE reminder_deliveries SET status=?, attempts=?", (status, attempts))
    before = plans(store, cid)
    roles.refresh()
    assert plans(store, cid) == before


async def test_four_slow_groups_start_before_window_expires_and_receipts_use_completion_time(store, monkeypatch):
    cid = competition()
    store.clock.now = NOW + timedelta(minutes=90)
    started = []
    all_started = asyncio.Event()
    monkeypatch.setattr(reminder, "_DELIVERY_SEND_TIMEOUT_SECONDS", 0.1)

    async def send(gid, text):
        started.append(gid)
        if len(started) == 4:
            all_started.set()
        await all_started.wait()
        store.clock.now = NOW + timedelta(minutes=90, seconds=55)
        return True

    assert await reminder.check_contest_start_reminders(send) == 4
    due = [p for p in plans(store, cid) if p[1] == "2026-11-10 11:30:00"]
    assert all(p[2:] == ("sent", 1) for p in due)
    assert store.conn.execute("SELECT DISTINCT sent_at FROM reminder_success_receipts").fetchall() == [("2026-11-10 11:30:55",)]
    assert await reminder.check_contest_start_reminders(send) == 0


async def test_one_claim_error_does_not_abort_other_groups(store, monkeypatch):
    competition()
    real_claim = reminder.claim_delivery

    def claim(gid, *args, **kwargs):
        if gid == GROUPS[0]:
            raise sqlite3.OperationalError("isolated failure")
        return real_claim(gid, *args, **kwargs)

    monkeypatch.setattr(reminder, "claim_delivery", claim)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(sender, now=NOW + timedelta(minutes=90)) == 3
    assert {c.args[0] for c in sender.await_args_list} == set(GROUPS[1:])


async def test_group_workers_are_bounded_ordered_and_cancelled_together():
    items = [{"group_id": str(i), "seq": j} for j in (0, 1) for i in range(6)]
    inflight = set()
    order = []
    four_started = asyncio.Event()
    release = asyncio.Event()
    peak = 0

    async def handle(item):
        nonlocal peak
        gid = item["group_id"]
        assert gid not in inflight
        inflight.add(gid)
        peak = max(peak, len(inflight))
        if len(inflight) == 4:
            four_started.set()
        try:
            await release.wait()
            order.append((gid, item["seq"]))
            return item["seq"]
        finally:
            inflight.remove(gid)

    task = asyncio.create_task(notifications.map_by_group(items, handle))
    await asyncio.wait_for(four_started.wait(), 1)
    release.set()
    assert await task == [item["seq"] for item in items]
    assert peak == 4
    for i in range(6):
        assert [seq for gid, seq in order if gid == str(i)] == [0, 1]
    four_started.clear()
    release.clear()
    task = asyncio.create_task(notifications.map_by_group(items, handle))
    await asyncio.wait_for(four_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not inflight


async def test_empty_and_unreadable_scope_report_failure_without_sending(store, monkeypatch):
    cid = competition(when="")
    app, api = bot()
    db.set_competition_notify_groups(cid, {"not-authorized"})
    await app._notify_publish_group({"fields_json": "{}"}, cid, "admin")
    api.post_group_message.assert_not_awaited()
    assert "没有已授权的目标群" in api.post_c2c_message.await_args.kwargs["content"]
    assert "已发送：0/0" not in main._notification_summary(0, 0)
    monkeypatch.setattr(db, "get_competition_target_groups", lambda cid: (_ for _ in ()).throw(sqlite3.OperationalError()))
    await app._notify_publish_group({"fields_json": "{}"}, cid, "admin")
    assert "通知范围读取失败" in api.post_c2c_message.await_args.kwargs["content"]


async def test_announcement_without_time_still_sends_and_reports_each_group(store):
    cid = competition(when="")
    db.set_competition_notify_groups(cid, {GROUPS[0], GROUPS[1]})
    app, api = bot()

    async def send(**kwargs):
        if kwargs["group_openid"] == GROUPS[1]:
            raise TimeoutError()
        return {"id": "accepted"}

    api.post_group_message.side_effect = send
    await app._notify_publish_group({"fields_json": "{}"}, cid, "admin")
    assert api.post_group_message.await_count == 2
    text = api.post_c2c_message.await_args.kwargs["content"]
    assert "1/2" in text and "timeout_unknown" in text
    assert db.describe_account("group", GROUPS[0]) in text
    assert db.describe_account("group", GROUPS[1]) in text
    assert plans(store, cid) == []


async def test_blocked_sender_times_out_and_does_not_hold_other_groups(store, monkeypatch):
    app, api = bot()
    completed = asyncio.Event()
    monkeypatch.setattr(main, "_ACTIVE_SEND_TIMEOUT_SECONDS", 0.05)

    async def send(**kwargs):
        if kwargs["group_openid"] == GROUPS[0]:
            await completed.wait()
            await asyncio.Event().wait()
        completed.set()
        return {"id": "accepted"}

    api.post_group_message.side_effect = send
    results = await app._send_group_notices([{"group_id": gid, "text": "notice"} for gid in GROUPS[:2]])
    assert completed.is_set()
    assert results[0][1].code == DeliveryCode.TIMEOUT_UNKNOWN and results[1][1].ok


async def test_private_notification_test_has_timeout_without_passive_fallback(store, monkeypatch):
    app, api = bot()
    monkeypatch.setattr(main, "_ACTIVE_SEND_TIMEOUT_SECONDS", 0.02)

    async def wait_forever(**kwargs):
        await asyncio.Event().wait()

    api.post_c2c_message.side_effect = wait_forever
    answer = await app._notification_test("admin", "私聊")
    assert "timeout_unknown" in answer
    api.post_c2c_message.assert_awaited_once()
    assert "msg_id" not in api.post_c2c_message.await_args.kwargs


async def test_general_reminder_slow_group_does_not_delay_other_groups(store, monkeypatch):
    for gid in GROUPS[:2]:
        db.create_collection(gid, "报名", deadline="2026-11-10 10:30")
    other_sent = asyncio.Event()
    monkeypatch.setattr(reminder, "_DELIVERY_SEND_TIMEOUT_SECONDS", 0.05)

    async def send(gid, text):
        if gid == GROUPS[0]:
            await other_sent.wait()
        else:
            other_sent.set()
        return True

    assert await reminder.check_general_reminders(send, now=NOW) == 2
    assert other_sent.is_set()


async def test_slow_approval_private_receipt_does_not_prevent_group_announcement(store, monkeypatch):
    app, api = bot()
    pid = db.create_pending_publication({"name": "无时间公告"}, GROUPS[0], "", "", "teacher")
    monkeypatch.setattr(main, "_ACTIVE_SEND_TIMEOUT_SECONDS", 0.02)

    async def wait_forever(**kwargs):
        await asyncio.Event().wait()

    api.post_c2c_message.side_effect = wait_forever
    assert await app._handle_publish_decision("admin", f"同意 {pid}")
    api.post_group_message.assert_awaited_once()
    assert api.post_group_message.await_args.kwargs["group_openid"] == GROUPS[0]


async def test_private_amend_actually_notifies_all_groups_including_admin_group(store):
    cid = competition()
    app, api = bot()
    await app.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id="admin", msg_id="incoming",
        content=f"/修正 {cid} summary=updated",
    ))
    assert db.get_competition(cid)["summary"] == "updated"
    assert {c.kwargs["group_openid"] for c in api.post_group_message.await_args_list} == set(GROUPS)
    assert all("msg_id" not in c.kwargs for c in api.post_group_message.await_args_list)
    assert all(c.kwargs["openid"] == "admin" for c in api.post_c2c_message.await_args_list)
    assert "4/4" in api.post_c2c_message.await_args.kwargs["content"]


@pytest.mark.parametrize("weekly", [False, True])
async def test_amend_notification_honors_designated_group_scope(store, weekly):
    if weekly:
        cid, _ = db.create_weekly_competition(
            "2026-11-09", {"name": "仅一群的周赛", "official_time": "2026-11-15 19:00"},
        )
    else:
        cid = competition()
        db.set_competition_notify_groups(cid, {GROUPS[0]})
    app, api = bot()
    await app.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id="admin", msg_id="incoming",
        content=f"/修正 {cid} summary=updated",
    ))
    api.post_group_message.assert_awaited_once()
    assert api.post_group_message.await_args.kwargs["group_openid"] == GROUPS[0]


async def test_permission_retry_is_persistent_exclusive_and_only_receipt_clears_it(store):
    app, api = bot()
    gid = GROUPS[0]
    db.block_group_notifications("qqbot", gid, "active_forbidden", 40034105)
    assert not await app._send_group_result(gid, "during cooldown")
    api.post_group_message.assert_not_awaited()
    store.clock.now += timedelta(minutes=5)
    started = asyncio.Event()
    release = asyncio.Event()

    async def send(**kwargs):
        started.set()
        await release.wait()
        return {"id": "accepted"}

    api.post_group_message.side_effect = send
    first = asyncio.create_task(app._send_group_result(gid, "automatic recovery"))
    await asyncio.wait_for(started.wait(), 1)
    db.init_db()
    restarted = main.ACMBot(app.client)
    assert not await restarted._send_group_result(gid, "concurrent retry")
    assert db.get_group_notification_block("qqbot", gid)
    release.set()
    assert (await first).ok
    assert db.get_group_notification_block("qqbot", gid) is None
    api.post_group_message.assert_awaited_once()


async def test_failed_recovery_keeps_cooldown_and_does_not_change_format(store):
    app, api = bot()
    gid = GROUPS[0]
    db.confirm_group_notification_channel("qqbot", gid, "markdown")
    db.block_group_notifications("qqbot", gid, "active_forbidden", 40034105)
    store.clock.now += timedelta(minutes=5)
    api.post_group_message.return_value = None
    assert not await app._send_group_result(gid, "unknown result")
    assert not await app._send_group_result(gid, "do not flood")
    api.post_group_message.assert_awaited_once()
    assert db.get_group_notification_block("qqbot", gid)
    assert db.get_group_notification_format("qqbot", gid) == "markdown"


async def test_next_valid_reminder_recovers_permission_without_manual_test(store):
    cid = competition()
    db.set_competition_notify_groups(cid, {GROUPS[0]})
    db.block_group_notifications("qqbot", GROUPS[0], "active_forbidden", 40034105)
    store.clock.now += timedelta(minutes=90)
    app, api = bot()
    assert await reminder.check_contest_start_reminders(app._send_group_result) == 1
    assert db.get_group_notification_block("qqbot", GROUPS[0]) is None
    assert plans(store, cid)[0][2:] == ("sent", 1)


def test_old_permission_schema_migrates_and_keeps_initial_cooldown(store):
    with store.conn:
        store.conn.execute("DROP TABLE group_notification_blocks")
        store.conn.execute("CREATE TABLE group_notification_blocks (protocol TEXT, group_id TEXT,"
                           " error_code TEXT, platform_code INTEGER, blocked_at TEXT, PRIMARY KEY(protocol, group_id))")
        store.conn.execute("INSERT INTO group_notification_blocks VALUES ('qqbot', ?, 'active_forbidden', 40034105, ?)",
                           (GROUPS[0], db.now_str()))
    db.init_db()
    assert db.group_notification_paused("qqbot", GROUPS[0])
    store.clock.now += timedelta(minutes=5)
    assert not db.group_notification_paused("qqbot", GROUPS[0])
    assert db.claim_group_notification_retry("qqbot", GROUPS[0])
    assert not db.claim_group_notification_retry("qqbot", GROUPS[0])
