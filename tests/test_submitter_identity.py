"""比赛发布人身份实时解析与超管身份隔离的单元测试。

覆盖验收点：
1) 超管发布 → 展示超管身份，即使老师表里存在同一 openid 也不被覆盖；
2) 已绑定老师发布 → 展示老师身份（含 QQ）；
3) 超管误发 /认领老师 → 直接被拦截，不写别名/备注/老师表；
4) 私聊 /发布 → 作用域绑定当前工作区群、提交人记为该超管、只回私聊不发群。
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, commands, config, llm, main, reminder_preflight, daily_digest
from cdutetc import database as db
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage

GROUP = "A0EF5D2D8BA4B3CC76E3C70C47971FDE"
OTHER = "B1C2D3E4F5A6B7C8D9E0F1A2B3C4D5E6"
ADMIN = "admin"
TEACHER = "t1"
NOW = datetime(2026, 11, 11, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture()
def memory_db(monkeypatch):
    """每个用例一套独立内存库，避免污染真实 contests.db。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {ADMIN})
    db.init_db()
    yield conn
    conn.close()


def _pending(submitted_by: str) -> dict:
    """构造一条待确认申请，用于校验确认卡片的「提交人」渲染。"""
    return {
        "id": 7,
        "group_id": GROUP,
        "name": "蓝桥杯省赛",
        "official_time": "2026-03-15 09:00",
        "fields_json": json.dumps({"name": "蓝桥杯省赛", "official_time": "2026-03-15 09:00"},
                                  ensure_ascii=False),
        "submitted_by": submitted_by,
        "duplicate_note": "",
    }


# ------------------------------------------------- 超管身份优先且不被老师表覆盖
def test_super_admin_submitter_shows_super_admin(memory_db):
    import cdutetc.roles as roles

    db.set_account_label("user", ADMIN, "刘灏锃")
    # 历史误操作把超管 openid 写进老师表：展示仍必须锁定为超管
    roles.add_teacher(ADMIN, operator=ADMIN, note="张老师")

    assert main._format_submitter(ADMIN) == "刘灏锃（超管）"
    review = main._build_publish_review(_pending(ADMIN))
    assert "提交人：刘灏锃（超管）" in review
    assert "老师" not in review.split("提交人：", 1)[1].splitlines()[0]


def test_super_admin_without_label_shows_placeholder(memory_db):
    assert main._format_submitter(ADMIN) == "超级管理员（ID: admin...）"
    assert "提交人：超级管理员（ID: admin...）" in main._build_publish_review(_pending(ADMIN))


# -------------------------------------------------------- 已绑定老师显示老师身份
def test_bound_teacher_submitter_shows_teacher(memory_db):
    import cdutetc.roles as roles

    db.set_entity_alias("user", TEACHER, "张老师", "10001")
    roles.add_teacher(TEACHER, operator=ADMIN, note="张老师")

    assert main._format_submitter(TEACHER) == "张老师（老师, QQ: 10001）"
    assert "提交人：张老师（老师, QQ: 10001）" in main._build_publish_review(_pending(TEACHER))


def test_unknown_submitter_falls_back(memory_db):
    stranger = "FFFFFFFF00000000AAAA"
    assert main._format_submitter(stranger) == "未知成员（ID: FFFFFFFF...）"
    assert main._format_submitter("") == "未知成员（无账号标识）"


def test_pending_list_uses_realtime_identity(memory_db):
    """待确认列表与确认卡片共用同一套实时身份解析，不出现两套口径。"""
    db.create_pending_publication(
        {"name": "蓝桥杯省赛", "official_time": "2026-03-15 09:00"},
        group_id=GROUP, source_url="", source_text="", submitted_by=ADMIN,
    )
    text = asyncio.run(commands.handle_command("/待确认列表", GROUP, ADMIN))[0]["text"]
    assert "提交人 超级管理员（ID: admin...）" in text


# ------------------------------------------------- 超管误发 /认领老师 必须被拦截
def test_super_admin_claim_teacher_is_rejected(memory_db):
    text = asyncio.run(commands.handle_command("/认领老师 10001 张老师", GROUP, ADMIN))[0]["text"]
    assert "当前账号为超级管理员" in text
    assert db.get_entity_alias("user", ADMIN) is None
    assert db.get_account_label("user", ADMIN) in (None, "")
    assert all(str(item.get("user_id")) != ADMIN for item in db.list_runtime_teachers())
    # 身份隔离后，超管展示仍是超管
    assert main._format_submitter(ADMIN) == "超级管理员（ID: admin...）"


# ------------------------------------- 私聊 /发布：绑定工作区群、只回私聊不发群
@pytest.fixture()
def bot_env(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    for module in (main, reminder_preflight, daily_digest):
        monkeypatch.setattr(module, "datetime", Clock)

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "submitter.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GROUP, OTHER})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {GROUP, OTHER})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {ADMIN})
    monkeypatch.setattr(config, "ADMIN_GROUP", GROUP)
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", True)

    admin_session.reset_all()
    db.init_db()
    db.set_account_label("group", GROUP, "集训队")
    db.set_account_label("group", OTHER, "集训队二群")

    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "sent-private"}),
                          post_group_message=AsyncMock(return_value={"id": "sent-group"}))
    adapter = QQBotClient("offline-app", "offline-secret")
    adapter._client = SimpleNamespace(api=api)
    bot = main.ACMBot(adapter)
    monkeypatch.setattr(llm, "_chat_once",
                        AsyncMock(side_effect=AssertionError("私聊发布不应直连模型")))
    yield SimpleNamespace(api=api, bot=bot)
    admin_session.reset_all()


async def test_private_publish_binds_active_group_and_replies_privately(bot_env):
    admin_session.set_active_group(ADMIN, OTHER)

    await bot_env.bot.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id=ADMIN,
        msg_id="private-inbound", content="/发布 蓝桥杯省赛 2026-03-15 09:00",
    ))

    pendings = db.list_pending_publications()
    assert len(pendings) == 1
    pending = pendings[0]
    # 作用域＝当前工作区群，提交人＝发消息的超管本人
    assert pending["group_id"] == OTHER
    assert pending["submitted_by"] == ADMIN

    # 确认卡片走超管身份解析，并经私聊送达
    sent = [call.kwargs["content"] for call in bot_env.api.post_c2c_message.await_args_list]
    assert any("提交人：超级管理员（ID: admin...）" in text for text in sent)
    # 回执全部走私聊，绝不向真实群发送
    assert bot_env.api.post_group_message.await_count == 0
