"""超管私聊目标工作群（Active Group）：绑定/切换/重置、单群视角注入、私聊沙箱预览。

覆盖验收点：
1) 超管 /切群 成功绑定、切换，/重置群 清除；
2) 非超管私聊 /切群 被直接拦截；
3) 绑定 A 群后私聊 /课程添加 正确入库到 A 群；
4) 绑定 A 群后 /晨报预览 只在私聊回显，绝不调用发群接口；
5) 切换群后 /比赛状态 返回目标群独立诊断，而非全群汇总。
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, commands, config, daily_digest
from cdutetc import database as db
from cdutetc import llm, main, reminder_preflight
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage

NOW = datetime(2026, 11, 11, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    for module in (main, reminder_preflight, daily_digest):
        monkeypatch.setattr(module, "datetime", Clock)

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "active_group.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"t1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ADMIN_GROUP", "team")
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "WEEKLY_CONTEST_TEMPLATE", "集训周赛")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 6)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "19:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "21:00")
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))

    admin_session.reset_all()
    db.init_db()
    db.set_account_label("group", "team", "集训队")
    db.set_account_label("group", "new", "集训队二群")

    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "sent-private"}),
                          post_group_message=AsyncMock(return_value={"id": "sent-group"}))
    adapter = QQBotClient("offline-app", "offline-secret")
    adapter._client = SimpleNamespace(api=api)
    bot = main.ACMBot(adapter)
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(side_effect=AssertionError("私聊管理不应调用模型")))

    async def send_private(content, user="admin"):
        await bot.on_private_message_create(QQBotMessage(
            None, message_type="private", user_id=user,
            msg_id="private-inbound", content=content,
        ))
        return api.post_c2c_message.call_args.kwargs["content"]

    yield SimpleNamespace(api=api, bot=bot, send_private=send_private)
    admin_session.reset_all()


async def test_super_admin_binds_switches_and_resets_group(env):
    text = await env.send_private("/切群 集训队二群")
    assert "已将操作群切换为" in text and "集训队二群" in text and "new" in text
    assert admin_session.get_active_group("admin") == "new"

    # 按群号切换
    text = await env.send_private("/切群 team")
    assert "team" in text
    assert admin_session.get_active_group("admin") == "team"

    # 不带参数列群：两个群都出现，并标出当前选定群
    text = await env.send_private("/选群")
    assert "集训队" in text and "集训队二群" in text and "✅ 当前" in text

    text = await env.send_private("/当前群")
    assert "当前操作群" in text and "team" in text

    text = await env.send_private("/重置群")
    assert "已解除" in text
    assert admin_session.get_active_group("admin") is None

    text = await env.send_private("/当前群")
    assert "未绑定操作群" in text
    env.api.post_group_message.assert_not_awaited()


async def test_switch_by_index_and_invalid_group_rejected(env):
    # 序号按群 openid 排序：1=new，2=team
    text = await env.send_private("/选群 1")
    assert "new" in text and admin_session.get_active_group("admin") == "new"
    text = await env.send_private("/选群 2")
    assert "team" in text and admin_session.get_active_group("admin") == "team"
    # 未授权的群不允许绑定，且不会污染已有绑定
    text = await env.send_private("/切群 999999")
    assert "超出范围" in text
    text = await env.send_private("/切群 不存在的群")
    assert "没有找到群" in text
    assert admin_session.get_active_group("admin") == "team"


async def test_non_super_admin_private_switch_is_blocked(env):
    text = await env.send_private("/切群 集训队二群", user="t1")
    assert "私聊管理仅对超级管理员开放" in text
    assert admin_session.get_active_group("t1") is None
    env.api.post_group_message.assert_not_awaited()


async def test_course_added_to_bound_group_only(env):
    await env.send_private("/切群 集训队二群")
    text = await env.send_private("/课程添加 算法训练 周三 19:00-21:00 机房")
    assert "已添加课程" in text

    assert db.list_training_sessions("new", active_only=True)
    assert db.list_training_sessions("team", active_only=True) == []
    env.api.post_group_message.assert_not_awaited()


async def test_digest_preview_renders_private_only(env):
    db.create_training_session("new", {"title": "算法训练", "weekday": "周三",
                                       "start_time": "19:00", "end_time": "21:00",
                                       "location": "机房"}, "admin")
    await env.send_private("/切群 new")
    env.api.post_c2c_message.reset_mock()

    text = await env.send_private("/晨报预览")
    # 私聊回执现在统一带工作区标牌首行（C5），正文事实另行断言。
    assert text.splitlines()[0].startswith("📌【当前工作区：")
    assert "【晨报私聊模拟预览】" in text
    assert "算法训练" in text
    assert "new" in text
    env.api.post_group_message.assert_not_awaited()


async def test_digest_preview_reports_skip_without_sending(env):
    await env.send_private("/切群 team")
    text = await env.send_private("/晨报预览")
    assert "晨报将跳过发送" in text
    env.api.post_group_message.assert_not_awaited()


async def test_contest_status_uses_bound_group_only(env):
    await env.send_private("/切群 new")
    text = await env.send_private("/比赛状态")
    # 单群视角：不再出现「私聊全局视图」的多群汇总
    assert "私聊全局视图" not in text
    assert "群：集训队二群" in text
    assert "群：集训队 " not in text
    env.api.post_group_message.assert_not_awaited()

    # 解除绑定后回到全局视图，两个群都出现
    await env.send_private("/重置群")
    text = await env.send_private("/比赛状态")
    assert "共 2 个已授权群" in text
    assert "群：集训队" in text and "群：集训队二群" in text


async def test_group_admin_defaults_to_bound_group(env):
    await env.send_private("/切群 集训队二群")
    text = await env.send_private("/群提醒")
    assert "【集训队二群主动提醒】" in text
    # 跨群总览仍走全局视角，不受绑定影响
    text = await env.send_private("/群配置 全部")
    assert "请在私聊中查询" not in text
    env.api.post_group_message.assert_not_awaited()


def test_resolve_group_arg_supports_index_name_and_id(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"a", "b"})
    monkeypatch.setattr(commands, "_group_label", lambda gid: {"a": "集训队", "b": "新生群"}[gid])
    assert commands.resolve_group_arg("1") == ("a", "")
    assert commands.resolve_group_arg("b") == ("b", "")
    assert commands.resolve_group_arg("新生群") == ("b", "")
    assert commands.resolve_group_arg("不存在")[0] is None


async def test_group_config_by_index_auto_binds_and_marks_header(env):
    """C5：私聊 /群配置 <序号> 解析成功后自动锁定工作区，并回带 📌 标牌。"""
    assert admin_session.get_active_group("admin") is None
    target, _ = commands.resolve_group_arg("1")
    assert target in config.ALLOWED_GROUPS

    text = await env.send_private("/群配置 1")
    assert admin_session.get_active_group("admin") == target
    first = text.splitlines()[0]
    assert first.startswith("📌【当前工作区：")
    # 标牌优先展示群备注名；未认领真实群号时不拼 openid，避免暴露短哈希。
    assert db.get_account_label("group", target) in first
    env.api.post_group_message.assert_not_awaited()


async def test_help_without_binding_shows_global_mode_header(env):
    """C5：未绑定工作区时 /帮助 首行为全局模式标牌。"""
    assert admin_session.get_active_group("admin") is None
    text = await env.send_private("/帮助")
    assert text.splitlines()[0] == "🌐【当前模式：全局模式 (未选群)】"
    # 标牌不影响正文帮助内容
    assert "/帮助 课程" in text
    env.api.post_group_message.assert_not_awaited()
