"""复现真实发布语句：时间解析、双协议确认、批准与主动提醒全链路。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database, llm, main, reminder
from cdutetc.clients.onebot import OneBotClient, OneBotMessage
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage
from cdutetc.publish_text import parse_simple_publication
from cdutetc.utils import match_publish_command

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 7, 9, 40, tzinfo=TZ)
USER_PHRASES = [
    "添加一个比赛今天上午10点15分开始",
    "添加一个比赛，今天上午10点15开始",
    "添加一个比赛，2026年11月7号上午10点15acm算法比赛开始",
    "2026年11月7号上午10点15，acm算法比赛正式开始",
    "现在做一个测试，10点15分会有一个比赛，你记得提醒我",
]


def group_message(protocol, text, member="admin"):
    if protocol == "qqbot":
        return QQBotMessage(None, message_type="group", group_id="g1", user_id=member,
                            content=text, msg_id="group-inbound", always_addressed=True)
    return OneBotMessage({"message_type": "group", "message_id": "group-inbound", "self_id": "bot",
                          "group_id": "g1", "user_id": member, "raw_message": f"[CQ:at,qq=bot] {text}"})


def private_message(protocol, text):
    if protocol == "qqbot":
        return QQBotMessage(None, message_type="private", user_id="admin", content=text, msg_id="approve-inbound")
    return OneBotMessage({"message_type": "private", "message_id": "approve-inbound", "user_id": "admin", "raw_message": text})


@pytest.fixture(params=["onebot", "qqbot"])
def app(request, monkeypatch, tmp_path):
    protocol = request.param
    monkeypatch.setattr(config, "BOT_PROTOCOL", protocol)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", True)
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", True)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "publish.sqlite"))
    clock = SimpleNamespace(current=NOW)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current if tz is None else clock.current.astimezone(tz)
    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(llm, "datetime", Clock)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or clock.current)
    database.init_db()
    chat = AsyncMock(side_effect=AssertionError("简单比赛发布不应调用模型"))
    qa = AsyncMock(side_effect=AssertionError("明确发布不能掉入普通问答"))
    classifier = AsyncMock(side_effect=AssertionError("明确发布不需要模型分类"))
    monkeypatch.setattr(llm, "_chat_once", chat)
    monkeypatch.setattr(llm, "classify_intent", classifier)
    monkeypatch.setattr(commands, "handle_non_command_message", qa)
    if protocol == "qqbot":
        api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "group-sent"}),
                              post_c2c_message=AsyncMock(return_value={"id": "private-sent"}))
        client = QQBotClient("offline-app", "offline-secret")
        client._client = SimpleNamespace(api=api)
        def private_calls():
            return [call.kwargs for call in api.post_c2c_message.await_args_list]
        def group_texts():
            return [call.kwargs["content"] for call in api.post_group_message.await_args_list]
    else:
        client = OneBotClient("ws://127.0.0.1:3001")
        client._ws = object()
        client.call_api = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 123}})
        def private_calls():
            return [{"openid": call.kwargs["user_id"], "content": call.kwargs["message"]}
                    for call in client.call_api.await_args_list if call.args[0] == "send_private_msg"]
        def group_texts():
            return [call.kwargs["message"] for call in client.call_api.await_args_list if call.args[0] == "send_group_msg"]
    return SimpleNamespace(bot=main.ACMBot(client), protocol=protocol, clock=clock, chat=chat, qa=qa,
                           classifier=classifier, private_calls=private_calls, group_texts=group_texts)


@pytest.mark.parametrize("phrase", USER_PHRASES)
async def test_user_phrases_create_real_pending_review_then_approval_and_reminder(app, phrase):
    await app.bot.on_group_at_message_create(group_message(app.protocol, phrase))
    pending = database.list_pending_publications()
    assert len(pending) == 1
    item = pending[0]
    assert item["official_time"] == "2026-11-07 10:15"
    assert item["group_id"] == "g1" and item["submitted_by"] == "admin"
    assert database.list_competitions() == []
    notices = app.private_calls()
    assert len(notices) == 1 and notices[0]["openid"] == "admin"
    assert "2026-11-07 10:15" in notices[0]["content"]
    assert f"同意 {item['id']}" in notices[0]["content"]
    assert "msg_id" not in notices[0]  # 群入站ID不能借给主动私聊。
    assert any("待超管确认" in text and "2026-11-07 10:15" in text for text in app.group_texts())
    await app.bot.on_private_message_create(private_message(app.protocol, f"同意 {item['id']}"))
    comp = database.list_competitions(status="confirmed")
    assert len(comp) == 1 and comp[0]["official_time"] == "2026-11-07 10:15"
    plans = database.list_deliveries("g1")
    assert {plan["scheduled_at"] for plan in plans} == {"2026-11-07 09:45:00", "2026-11-07 10:00:00"}
    assert database.list_deliveries("g2") == []
    due = NOW.replace(hour=9, minute=45)
    assert await reminder.check_contest_start_reminders(app.bot._send_group_result, now=due) == 1
    assert await reminder.check_contest_start_reminders(app.bot._send_group_result, now=due) == 0
    assert any("开赛提醒" in text and "2026-11-07 10:15" in text for text in app.group_texts())
    app.chat.assert_not_awaited()
    app.qa.assert_not_awaited()
    app.classifier.assert_not_awaited()


async def test_explicit_natural_command_does_not_depend_on_llm_intent_switch(app, monkeypatch):
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    await app.bot.on_group_at_message_create(group_message(app.protocol, USER_PHRASES[0]))
    assert database.list_pending_publications()[0]["official_time"] == "2026-11-07 10:15"


async def test_late_approval_only_schedules_future_node(app):
    app.clock.current = NOW.replace(hour=9, minute=46)
    await app.bot.on_group_at_message_create(group_message(app.protocol, USER_PHRASES[2]))
    pid = database.list_pending_publications()[0]["id"]
    await app.bot.on_private_message_create(private_message(app.protocol, f"同意 {pid}"))
    assert [row["scheduled_at"] for row in database.list_deliveries("g1")] == ["2026-11-07 10:00:00"]


async def test_ordinary_member_cannot_publish_or_trigger_review(app):
    await app.bot.on_group_at_message_create(group_message(app.protocol, USER_PHRASES[0], member="student"))
    assert database.list_pending_publications() == []
    assert app.private_calls() == []
    app.chat.assert_not_awaited()


@pytest.mark.parametrize("text", ["添加一个比赛怎么操作", "添加一个比赛什么时候开始", "10点15分比赛会开始吗", "不要添加一个比赛今天10点开始"])
def test_queries_and_negated_requests_are_not_explicit_publish_commands(text):
    assert match_publish_command(text) is None


@pytest.mark.parametrize("text,expected", [
    ("今天上午10点15开始", "2026-11-07 10:15"),
    ("明天下午三点半开始", "2026-11-08 15:30"),
    ("后天晚上8点比赛开始", "2026-11-09 20:00"),
    ("2026年11月7号上午10点15acm算法比赛开始", "2026-11-07 10:15"),
    ("明早10:15比赛开始", "2026-11-08 10:15"),
    ("下周一上午9点比赛开始", "2026-11-09 09:00"),
])
def test_chinese_and_relative_time_are_beijing_based(text, expected):
    assert parse_simple_publication(text, now=NOW.astimezone(timezone.utc))["contest_time"] == expected


@pytest.mark.parametrize("text", ["今天25点比赛开始", "今天上午10点70比赛开始", "2026年2月30号10点比赛开始"])
def test_invalid_dates_and_clocks_are_not_silently_recorded(text):
    with pytest.raises(ValueError):
        parse_simple_publication(text, now=NOW)


async def test_complex_extraction_gets_beijing_reference_and_normalizes_relative_time(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(llm, "datetime", Clock)
    chat = AsyncMock(return_value='{"name":"校赛","contest_time":"今天上午10点15分"}')
    monkeypatch.setattr(llm, "_chat_once", chat)
    info, error = await llm.extract_contest("校赛今天上午10点15分开始，设备要求见老师通知")
    assert error is None and info["contest_time"] == "2026-11-07 10:15"
    assert "当前北京时间：2026-11-07 09:40" in chat.await_args.args[0]
