"""阶段三：征集登记与统计（collections / collection_responses）、个人幂等报名/取消/查询、
管理员明细 + 普通成员汇总、公共比赛全员提醒、持久化提醒去重与失败重试（内存 SQLite 离线测试）。"""

import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
import database
import reminder

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


def _now(hour=12, minute=0):
    return datetime(2026, 11, 1, hour, minute, tzinfo=TZ)


# ---------- 征集创建与校验 ----------


def test_collection_create_and_validate(db):
    aid = database.create_collection(
        "g1", "国庆集训报名", purpose="报名意向", deadline="2026-11-05 23:59",
        capacity=10, fields=["姓名", "方向"], operator="t")
    a = database.get_collection(aid)
    assert a["status"] == "open" and a["capacity"] == 10
    assert a["fields_json"]
    with pytest.raises(ValueError):
        database.create_collection("g1", "", purpose="标题为空")
    with pytest.raises(ValueError):
        database.create_collection("g1", "bad", fields=["ok", 123])


def test_declared_fields_validation(db):
    aid = database.create_collection("g1", "报名", fields=["姓名"])
    ok, msg = database.register_collection(aid, "g1", "m1", {"手机号": "123"})
    assert not ok and "不支持字段" in msg
    ok, msg = database.register_collection(aid, "g1", "m1", {"姓名": 123})
    assert not ok and "应为文本" in msg


# ---------- 个人幂等报名 / 取消 / 查询 ----------


def test_register_idempotent_and_update(db):
    aid = database.create_collection("g1", "集训报名", fields=["姓名"])
    ok, msg = database.register_collection(aid, "g1", "m1", {"姓名": "张三"})
    assert ok
    ok, msg = database.register_collection(aid, "g1", "m1", {"姓名": "张三丰"})
    assert ok and "更新" in msg
    # 同一人多次报名只保留一条（不重复计数）
    assert len(database.list_collection_responses(aid)) == 1
    assert "张三丰" in database.get_collection_registration(aid, "m1")["data_json"]


def test_cancel_and_register_again(db):
    aid = database.create_collection("g1", "报名")
    database.register_collection(aid, "g1", "m1", {})
    assert database.cancel_collection_registration(aid, "g1", "m1")[0] is True
    assert database.get_collection_registration(aid, "m1") is None
    assert database.summarize_collection(aid)["registered"] == 0
    assert database.cancel_collection_registration(aid, "g1", "m1")[0] is False  # 已无报名
    ok, msg = database.register_collection(aid, "g1", "m1", {})
    assert ok and "重新报名" in msg
    assert database.get_collection_registration(aid, "m1") is not None


def test_capacity_limit(db):
    aid = database.create_collection("g1", "限2人", capacity=2)
    database.register_collection(aid, "g1", "m1", {})
    database.register_collection(aid, "g1", "m2", {})
    ok, msg = database.register_collection(aid, "g1", "m3", {})
    assert not ok and "名额" in msg


def test_collection_cross_group_denied(db):
    aid = database.create_collection("g1", "仅本群征集")
    ok, msg = database.register_collection(aid, "g2", "m1", {})
    assert not ok and "本群" in msg
    assert database.cancel_collection_registration(aid, "g2", "m1")[0] is False
    assert database.get_collection_registration(aid, "m1") is None


def test_close_collection_blocks_register(db):
    aid = database.create_collection("g1", "报名")
    database.register_collection(aid, "g1", "m1", {})
    assert database.set_collection_status(aid, "closed", operator="t") is True
    ok, msg = database.register_collection(aid, "g1", "m2", {})
    assert not ok and "结束" in msg
    assert database.get_collection(aid)["status"] == "closed"


# ---------- 管理员明细 + 普通成员汇总 ----------


def test_summary_vs_detail(db):
    aid = database.create_collection("g1", "报名", capacity=4)
    for m in ["m1", "m2", "m3", "m4"]:
        ok, _ = database.register_collection(aid, "g1", m, {})
        assert ok
    ok, _ = database.register_collection(aid, "g1", "m5", {})  # 容量已满
    assert not ok
    database.cancel_collection_registration(aid, "g1", "m4")
    summary = database.summarize_collection(aid)
    assert summary["registered"] == 3
    assert summary["cancelled"] == 1
    assert summary["remaining"] == 1
    assert "member_id" not in str(summary)  # 普通成员汇总不含个人明细
    detail = database.list_collection_responses(aid)
    assert len(detail) == 3
    assert {r["member_id"] for r in detail} == {"m1", "m2", "m3"}


# ---------- 持久化提醒去重与失败重试 ----------


def test_delivery_dedup_key_idempotent(db):
    assert database.ensure_delivery("g1", "collection", 1, "deadline", "2026-11-05 23:59") is True
    assert database.ensure_delivery("g1", "collection", 1, "deadline", "2026-11-05 23:59") is False
    d = database.get_delivery_by_key(
        database.delivery_dedup_key("g1", "collection", 1, "deadline", "2026-11-05 23:59"))
    assert d is not None and d["status"] == "pending"


def test_record_delivery_only_success_marks_sent(db):
    database.ensure_delivery("g1", "collection", 1, "deadline", "2026-11-05 23:59")
    key = database.delivery_dedup_key("g1", "collection", 1, "deadline", "2026-11-05 23:59")
    database.record_delivery_attempt(key, ok=False, error="网络错误")
    d = database.get_delivery_by_key(key)
    assert d["status"] == "pending" and d["attempts"] == 1 and d["last_error"]
    database.record_delivery_attempt(key, ok=True)
    d = database.get_delivery_by_key(key)
    assert d["status"] == "sent" and d["sent_at"]
    assert key not in [x["dedup_key"] for x in database.list_pending_deliveries()]


def test_general_reminder_success_marks_sent(db):
    database.create_collection("g1", "集训报名", deadline="2026-11-01 23:59")
    sent_log = []

    async def send(gid, text):
        sent_log.append((gid, text))
        return True

    n = asyncio.run(reminder.check_general_reminders(send, now=_now()))
    assert n == 1
    assert sent_log[0][0] == "g1" and "集训报名" in sent_log[0][1]
    # 已 sent，再次扫描不重复发送
    sent_log.clear()
    n2 = asyncio.run(reminder.check_general_reminders(send, now=_now()))
    assert n2 == 0 and sent_log == []


def test_general_reminder_retry_on_failure(db):
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    calls = []

    async def send_fail(gid, text):
        calls.append(gid)
        return False

    n = asyncio.run(reminder.check_general_reminders(send_fail, now=_now(), max_attempts=3))
    assert n == 0 and len(calls) == 1
    pending = database.list_pending_deliveries()
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert pending[0]["attempts"] == 1  # 失败留 pending 且计数

    async def send_ok(gid, text):
        return True

    n2 = asyncio.run(reminder.check_general_reminders(send_ok, now=_now(), max_attempts=3))
    assert n2 == 1
    assert len(database.list_pending_deliveries()) == 0  # 成功后清出队列


def test_general_reminder_gives_up_after_max_attempts(db):
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")

    async def send_fail(gid, text):
        return False

    for _ in range(3):
        asyncio.run(reminder.check_general_reminders(send_fail, now=_now(), max_attempts=2))
    # 第 3 轮不再尝试（attempts 已达上限）
    assert database.list_pending_deliveries() == []
    d = database.list_deliveries("g1")
    assert len(d) == 1 and d[0]["status"] == "failed" and d[0]["attempts"] == 2


def test_competition_reminder_all_groups(db, monkeypatch):
    """v2：已确认公共比赛提醒发送到所有允许群（原订阅群机制已移除）。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    cid = database.create_competition(
        {"name": "ICPC 2026", "official_reg_deadline": "2026-11-02 10:00",
         "official_time": "2026-11-03 09:00", "official_site": "https://icpc.example"},
        status="confirmed")
    sent_log = []

    async def send(gid, text):
        sent_log.append(gid)
        return True

    n = asyncio.run(reminder.check_general_reminders(send, now=_now()))
    # 距注册截止 22h / 距开赛 21h，均 <= REMIND_BEFORE_HOURS（默认 24h 内应触发）
    # 去重键含群号：每个允许群各应收到一条
    assert n == 2
    assert sorted(sent_log) == ["g1", "g2"]
