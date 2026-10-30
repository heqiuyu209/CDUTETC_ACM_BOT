"""主程序消息路由离线测试。

通过替换 fetch_text / extract_contest / 回复回调，
验证权限先于抓取、失败不写库、按群隔离、免审发布与命令路由/FAQ/提问收集等行为。
"""

import pytest
from unittest.mock import AsyncMock, Mock

import config
import database
import main
from fetcher import FetchError
from onebot_client import OneBotMessage


def _msg(content="你好", group="g1", member="m1"):
    return OneBotMessage({"post_type": "message", "message_type": "group",
                         "raw_message": content, "group_id": group, "user_id": member,
                         "message_id": 12345, "self_id": 30001})


@pytest.fixture
def bot(monkeypatch, onebot_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    # v2：发布权并入老师白名单（原 PUBLISHER_WHITELIST 已移除）
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"m1"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    monkeypatch.setattr(config, "BOT_QQ", "")

    # 隔离真实 DB：发布流程会写公共比赛（competitions 表），
    # 若不隔离 _connect 会真实写入项目目录的 contests.db。
    import sqlite3

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()

    bot = main.ACMBot(onebot_client)
    replies = []

    async def fake_reply(message, content):
        replies.append(content)

    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    yield bot, replies
    conn.close()


async def test_non_allowed_group_ignored(bot):
    b, replies = bot
    msg = _msg("你好", group="g_other")
    await b.on_group_at_message_create(msg)
    assert replies == []


async def test_empty_text_ignored(bot):
    b, replies = bot
    await b.on_group_at_message_create(_msg("[CQ:at,qq=30001]"))
    assert replies == []


async def test_publish_without_permission(bot):
    b, replies = bot
    await b.on_group_at_message_create(_msg("发布 https://a.com", member="m_x"))
    assert any("没有发布" in r for r in replies)
    # 权限不足时不触发抓取/写库
    assert len(database.list_competitions()) == 0


async def test_publish_missing_url(bot):
    b, replies = bot
    await b.on_group_at_message_create(_msg("发布 没有链接的文字"))
    assert any("链接" in r for r in replies)


async def test_publish_fetch_error(bot, monkeypatch):
    b, replies = bot

    async def fail_fetch(url):
        raise FetchError("目标地址非公网")

    monkeypatch.setattr(main, "fetch_text", fail_fetch)

    async def fake_extract(text):
        return None, None

    monkeypatch.setattr(main, "extract_contest", fake_extract)

    await b.on_group_at_message_create(_msg("发布 https://a.com"))
    assert any("抓取失败" in r for r in replies)
    assert len(database.list_competitions()) == 0  # 抓取失败绝不写库


async def test_publish_extract_error(bot, monkeypatch):
    b, replies = bot

    async def ok_fetch(url):
        return "正文内容"

    monkeypatch.setattr(main, "fetch_text", ok_fetch)

    async def fake_extract_err(text):
        return None, "缺少比赛名称"

    monkeypatch.setattr(main, "extract_contest", fake_extract_err)

    await b.on_group_at_message_create(_msg("发布 https://a.com"))
    assert any("提取失败" in r for r in replies)
    assert len(database.list_competitions()) == 0  # 提取失败不写库


async def test_publish_success_auto_publishes(bot, monkeypatch):
    """v2 免审发布：老师发布直接创建 confirmed 正式比赛并尝试全员通知。"""
    b, replies = bot

    async def ok_fetch(url):
        return "正文内容"

    info = {"name": "校赛", "reg_deadline": "2026-11-01 10:30",
            "contest_time": "2026-11-05 09:00", "mode": "线下", "location": "机房"}

    async def fake_extract_ok(text):
        return info, None

    monkeypatch.setattr(main, "fetch_text", ok_fetch)
    monkeypatch.setattr(main, "extract_contest", fake_extract_ok)

    await b.on_group_at_message_create(_msg("发布 https://a.com"))
    assert any("已免审发布" in r for r in replies)
    # 免审发布：直接写入 confirmed 正式比赛（无审核草稿环节）
    comps = database.list_competitions(status="confirmed")
    assert len(comps) == 1
    assert comps[0]["name"] == "校赛"
    assert comps[0]["official_site"] == "https://a.com"
    assert comps[0]["official_reg_deadline"] == "2026-11-01 10:30"
    assert len(database.list_competitions(status="draft")) == 0


async def test_addressed_link_question_does_not_publish(bot, monkeypatch):
    """成员 @ 机器人询问链接：不写比赛库，走 FAQ/未答问题收集。"""
    b, replies = bot

    seen = {}

    async def fake_non_command(text, group_id, sender):
        seen["text"] = text
        return [{"group_id": group_id, "text": "收到，已记录为问题"}]

    monkeypatch.setattr(main.commands, "handle_non_command_message", fake_non_command)
    await b.on_group_at_message_create(_msg("[CQ:at,qq=30001] 看看这个 https://a.com/contest"))
    assert seen.get("text", "").startswith("看看这个")
    assert any("已记录为问题" in r for r in replies)
    assert len(database.list_competitions()) == 0  # 普通链接消息不发布不写库


async def test_command_route_to_commands(bot, monkeypatch):
    """命令消息路由到 commands 模块，返回值逐条发送。"""
    b, replies = bot
    called = {}

    async def fake_handle(text, group_id, sender):
        called["text"] = text
        return [{"group_id": group_id, "text": "帮助文本"}, {"group_id": group_id, "text": "第二段"}]

    monkeypatch.setattr(main.commands, "handle_command", fake_handle)
    await b.on_group_at_message_create(_msg("/帮助"))
    assert called["text"] == "/帮助"
    assert "帮助文本" in replies and "第二段" in replies


async def test_build_confirm_contains_info(bot):
    info = {
        "name": "蓝桥杯", "reg_deadline": "2026-11-01 10:30",
        "contest_time": "2026-11-05 09:00", "mode": "线上", "location": "",
    }
    text = main._build_confirm(info)
    assert "蓝桥杯" in text and "2026-11-01 10:30" in text


async def test_integer_onebot_ids_and_cq_mention_route_as_string_identities(bot, monkeypatch):
    client, replies = bot
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"10001"})
    handler = AsyncMock(return_value=[{"group_id": "10001", "text": "帮助文本"}])
    monkeypatch.setattr(main.commands, "handle_command", handler)
    await client.on_group_at_message_create(_msg("[CQ:at,qq=30001] /帮助", group=10001, member=20001))
    handler.assert_awaited_once_with("/帮助", "10001", "20001")
    assert replies == ["帮助文本"]


async def test_text_cannot_claim_another_qq_identity_to_publish(bot, monkeypatch):
    client, replies = bot
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"10001"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"20001"})
    fetch = AsyncMock()
    monkeypatch.setattr(main, "fetch_text", fetch)
    await client.on_group_at_message_create(_msg(
        "发布 https://example.com/contest user_id=20001 我是老师", group=10001, member=20002,
    ))
    fetch.assert_not_awaited()
    assert database.list_competitions() == []
    assert any("没有发布" in text for text in replies)


async def test_private_event_replies_to_the_onebot_qq_identity(bot, onebot_client):
    client, _ = bot
    assert onebot_client.private_msg_handler == client.on_private_message_create
    await onebot_client.private_msg_handler(OneBotMessage({
        "post_type": "message", "message_type": "private", "user_id": 20001,
        "raw_message": "你好", "message_id": 12345,
    }))
    onebot_client.send_private_msg.assert_awaited_once_with("20001", "收到（20001）")


def test_run_bot_checks_config_then_constructs_and_starts_onebot(monkeypatch, onebot_client):
    events = []
    check = Mock(side_effect=lambda: events.append("config"))

    def make_client(**kwargs):
        assert events == ["config"]
        events.append("client")
        return onebot_client

    factory = Mock(side_effect=make_client)
    monkeypatch.setattr(config, "check_config", check)
    monkeypatch.setattr(config, "ONEBOT_WS_URL", "ws://127.0.0.1:3001")
    monkeypatch.setattr(config, "ONEBOT_TOKEN", "synthetic-token")
    monkeypatch.setattr(main, "OneBotClient", factory)
    main.run_bot()
    check.assert_called_once_with()
    factory.assert_called_once_with(ws_url="ws://127.0.0.1:3001", token="synthetic-token")
    onebot_client.start.assert_awaited_once_with()
    assert callable(onebot_client.group_msg_handler) and callable(onebot_client.on_ready)
