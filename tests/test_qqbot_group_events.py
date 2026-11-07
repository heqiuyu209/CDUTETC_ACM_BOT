"""模拟官方网关生命周期：新旧群事件、@ 标记、授权和被动回复，不连接 QQ。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import commands, config, database, llm, main
from cdutetc.clients import qqbot


def body(content="你好", *, addressed=True, group="group-openid", mid="incoming-id"):
    return {
        "id": mid, "group_openid": group, "content": content,
        "author": {"member_openid": "member-openid"},
        "mentions": [{"id": "bot-openid", "is_you": True}] if addressed else [],
    }


def model(data):
    return SimpleNamespace(**{**data, "author": SimpleNamespace(**data["author"]),
                              "mentions": [SimpleNamespace(**item) for item in data["mentions"]]})


@pytest.fixture
def sdk(monkeypatch):
    """复制 SDK 的登录→连接解析器→ws_dispatch 顺序，禁止任何联网。"""
    async def run(adapter, scenario, *, native=False):
        class Client:
            def __init__(self, *, intents):
                assert intents.public_messages
                self.api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent-id"}))
                self.tasks = []

            async def _bot_login(self, token):
                def parse_at(payload):
                    self.ws_dispatch("group_at_message_create", model(payload["d"]))
                parsers = {"group_at_message_create": parse_at}
                if native:
                    def parse_group(payload):
                        self.ws_dispatch("group_message_create", model(payload["d"]))
                    parsers["group_message_create"] = parse_group
                self.native_parser = parsers.get("group_message_create")
                self._connection = SimpleNamespace(parser=parsers)

            def ws_dispatch(self, event, message):
                self.tasks.append(asyncio.create_task(getattr(self, "on_" + event)(message)))

            async def feed(self, event, data):
                payload = {"t": event.upper(), "id": "event-id", "d": data}
                self._connection.parser[event](payload)
                tasks, self.tasks = self.tasks, []
                await asyncio.gather(*tasks)

            async def start(self, **credentials):
                await self._bot_login("offline-token")
                assert "group_message_create" in self._connection.parser
                if native:
                    assert self._connection.parser["group_message_create"] is self.native_parser
                await scenario(self)

        monkeypatch.setattr(qqbot, "_import_botpy", lambda: SimpleNamespace(Client=Client, Intents=SimpleNamespace))
        await adapter.start()
        assert adapter._client is None
    return run


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"group-openid"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"group-openid"})
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin-openid"})
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_USE_LLM", False)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "NOTICE_UNAUTHORIZED_GROUP", True)
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "BOT_QQ", "")
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "events.sqlite3"))
    database.init_db()
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(side_effect=AssertionError("不得调用模型")))
    adapter = qqbot.QQBotClient("offline-appid", "offline-secret")
    return adapter, main.ACMBot(adapter)


@pytest.mark.parametrize("native", [False, True])
async def test_new_event_reaches_real_hello_route_with_passive_receipt(app, sdk, native):
    adapter, _ = app
    async def scenario(client):
        await client.feed("group_message_create", body("<@!bot-openid> 你好"))
        client.api.post_group_message.assert_awaited_once()
        sent = client.api.post_group_message.call_args.kwargs
        assert sent["group_openid"] == "group-openid"
        assert sent["msg_id"] == "incoming-id"  # 不是外层 event-id，也不能转主动发送。
        assert "你好" in sent["content"] or "嗨" in sent["content"]
        assert database.list_unanswered("group-openid") == []
    await sdk(adapter, scenario, native=native)


@pytest.mark.parametrize("mentions", [[], [{"id": "other", "is_you": False}], [{"id": "other", "is_you": "false"}]])
async def test_full_group_messages_without_bot_mention_are_silent(app, sdk, mentions):
    adapter, _ = app
    async def scenario(client):
        data = body("你好", addressed=False)
        data["mentions"] = mentions
        await client.feed("group_message_create", data)
        client.api.post_group_message.assert_not_awaited()
        assert database.list_unanswered("group-openid") == []
    await sdk(adapter, scenario)


@pytest.mark.parametrize("first", ["group_at_message_create", "group_message_create"])
async def test_same_message_in_new_and_legacy_events_only_replies_once(app, sdk, first):
    adapter, _ = app
    async def scenario(client):
        second = "group_message_create" if first == "group_at_message_create" else "group_at_message_create"
        await client.feed(first, body())
        await client.feed(second, body())
        client.api.post_group_message.assert_awaited_once()
    await sdk(adapter, scenario)


async def test_unaddressed_event_does_not_suppress_later_legacy_at_message(app, sdk):
    adapter, _ = app
    async def scenario(client):
        await client.feed("group_message_create", body(addressed=False))
        await client.feed("group_at_message_create", body(addressed=False))
        client.api.post_group_message.assert_awaited_once()
    await sdk(adapter, scenario)


async def test_new_group_gets_copyable_openid_authorization_command(app, sdk):
    adapter, _ = app
    async def scenario(client):
        await client.feed("group_message_create", body("/帮助", group="new-group-openid"))
        sent = client.api.post_group_message.call_args.kwargs
        assert "/加群 new-group-openid" in sent["content"]
        assert sent["group_openid"] == "new-group-openid" and sent["msg_id"] == "incoming-id"
        assert "new-group-openid" not in config.ALLOWED_GROUPS
    await sdk(adapter, scenario)


async def test_blacklisted_member_stays_silent_for_new_event(app, sdk, monkeypatch):
    adapter, _ = app
    monkeypatch.setattr(config, "QA_BLACKLIST", {"member-openid"})
    async def scenario(client):
        await client.feed("group_message_create", body())
        client.api.post_group_message.assert_not_awaited()
    await sdk(adapter, scenario)


@pytest.mark.parametrize("protocol,target,accepted", [
    ("qqbot", "662657785", False),
    ("qqbot", "A" * 32, True),
    ("qqbot", "1" * 32, True),
    ("onebot", "662657785", True),
])
def test_add_group_rejects_qq_number_only_for_official_protocol(app, monkeypatch, protocol, target, accepted):
    monkeypatch.setattr(config, "BOT_PROTOCOL", protocol)
    replies = commands.cmd_group_add("private", "admin-openid", target)
    assert (target in config.ALLOWED_GROUPS) is accepted
    assert bool(database.list_runtime_groups()) is accepted
    if not accepted:
        assert "QQ 群号" in replies[0]["text"]
        assert not replies[0].get("notify_super_admin")


async def test_group_dedup_is_bounded_and_scoped_by_group():
    adapter = qqbot.QQBotClient("offline", "offline")
    adapter.group_msg_handler = AsyncMock()
    for i in range(1030):
        await adapter._receive_group_message(qqbot.wrap_group_at_message(body(mid=str(i))))
    await adapter._receive_group_message(qqbot.wrap_group_message(body(group="another-group", mid="1029")))
    assert adapter.group_msg_handler.await_count == 1031
    assert len(adapter._recent_group_messages) == 1024
