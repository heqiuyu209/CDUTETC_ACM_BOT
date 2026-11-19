"""清理命令的选群、身份权限、参数与私聊出口。"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import cdutetc
from cdutetc import admin_session, commands, config, database as db, group_admin, llm, main
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cleanup_commands.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "other"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "other"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ADMIN_GROUP", "team")
    admin_session.reset_all()
    db.init_db()

    cleanup = SimpleNamespace(
        preview_cleanup=Mock(return_value={
            "token": "confirm-code", "items": [{"type": "competition", "id": 7, "title": "旧赛"}],
            "skipped": [],
        }),
        apply_cleanup=Mock(return_value={"cleared": {"competition": 1}, "skipped": [], "backup": None}),
        mark_test=Mock(), unmark_test=Mock(), restore_competition=Mock(return_value=True),
        set_rule=Mock(), get_rules=Mock(return_value={
            "expired_contests": {"days": 30, "enabled": False},
            "test_data": {"days": 1, "enabled": True},
        }), run_rules=Mock(return_value={}),
    )
    monkeypatch.setattr(cdutetc, "group_cleanup", cleanup, raising=False)
    monkeypatch.setitem(sys.modules, "cdutetc.group_cleanup", cleanup)
    default_workspace = Mock(side_effect=AssertionError("清理不得使用默认群"))
    monkeypatch.setattr(group_admin, "active_workspace", default_workspace)
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(side_effect=AssertionError("清理不应调用模型")))

    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "private-sent"}),
                          post_group_message=AsyncMock(return_value={"id": "group-sent"}))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    bot = main.ACMBot(client)
    yield SimpleNamespace(cleanup=cleanup, api=api, bot=bot, default_workspace=default_workspace)
    admin_session.reset_all()


async def test_private_unselected_group_rejected_despite_default_and_external_target(env):
    result = await commands.handle_command("/清理预览 过期比赛", "team", "admin",
                                           private=True, active_group="other")
    assert "请先 /选群" in result[0]["text"]
    env.cleanup.preview_cleanup.assert_not_called()
    env.default_workspace.assert_not_called()


async def test_private_selected_group_overrides_external_target(env):
    admin_session.set_active_group("admin", "team")
    result = await commands.handle_command("/清理预览 过期比赛 45天", "other", "admin",
                                           private=True, active_group="other")
    env.cleanup.preview_cleanup.assert_called_once_with("team", "expired_contests", days=45, operator="admin")
    assert result[0]["group_id"] == "team"
    assert "/清理确认 confirm-code" in result[0]["text"]
    env.default_workspace.assert_not_called()


async def test_private_selected_but_revoked_group_rejected(env, monkeypatch):
    admin_session.set_active_group("admin", "other")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team"})
    result = await commands.handle_command("/清理预览 测试数据", "team", "admin", private=True)
    assert "未授权" in result[0]["text"]
    env.cleanup.preview_cleanup.assert_not_called()


@pytest.mark.parametrize("sender,private,group_id", [
    ("member", False, "team"), ("teacher", True, "team"), ("admin", False, "unknown"),
])
async def test_unauthorized_cleanup_never_reaches_backend(env, sender, private, group_id):
    await commands.handle_command("/清理预览 测试数据", group_id, sender, private=private)
    env.cleanup.preview_cleanup.assert_not_called()


async def test_teacher_can_preview_current_group(env):
    result = await commands.handle_command("/清理预览 测试数据", "team", "teacher")
    env.cleanup.preview_cleanup.assert_called_once_with("team", "test_data", days=0, operator="teacher")
    assert "预览" in result[0]["text"]
    assert "超管在本群重新预览" in result[0]["text"]
    assert "confirm-code" not in result[0]["text"]


async def test_assigned_group_teacher_can_preview_but_other_teacher_is_rejected(env, monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher", "other-teacher"})
    db.set_group_teachers("team", {"teacher"}, operator="admin")
    result = await commands.handle_command("/清理预览 过期比赛", "team", "other-teacher")
    assert "仅对本群老师" in result[0]["text"]
    env.cleanup.preview_cleanup.assert_not_called()
    await commands.handle_command("/清理预览 过期比赛", "team", "teacher")
    env.cleanup.preview_cleanup.assert_called_once_with("team", "expired_contests", days=30, operator="teacher")


@pytest.mark.parametrize("content", [
    "/清理确认 confirm-code", "/标记测试 比赛 7", "/取消测试标记 比赛 7", "/恢复比赛 7",
    "/清理规则", "/清理规则 测试数据 1天 开启",
])
async def test_teacher_cannot_write_or_manage_rules(env, content):
    result = await commands.handle_command(content, "team", "teacher")
    assert "仅对超级管理员" in result[0]["text"]
    for name in ("apply_cleanup", "mark_test", "unmark_test", "restore_competition", "set_rule", "get_rules"):
        getattr(env.cleanup, name).assert_not_called()


@pytest.mark.parametrize("content,name,args,kwargs", [
    ("/清理确认 confirm-code", "apply_cleanup", ("team", "confirm-code"), {"operator": "admin"}),
    ("/标记测试 课程 3", "mark_test", ("team", "training_session", 3), {"operator": "admin"}),
    ("/标记测试 提醒 3", "mark_test", ("team", "custom_reminder", 3), {"operator": "admin"}),
    ("/标记测试 旧比赛 3", "mark_test", ("team", "legacy_contest", 3), {"operator": "admin"}),
    ("/标记测试 发布申请 3", "mark_test", ("team", "publication", 3), {"operator": "admin"}),
    ("/取消测试标记 FAQ 3", "unmark_test", ("team", "faq", 3), {"operator": "admin"}),
    ("/恢复比赛 7", "restore_competition", ("team", 7), {"operator": "admin"}),
    ("/清理规则 测试数据 1天 开启", "set_rule", ("team", "test_data", 1, True), {"operator": "admin"}),
    ("/清理规则 过期比赛 30天 关闭", "set_rule", ("team", "expired_contests", 30, False), {"operator": "admin"}),
])
async def test_super_admin_routes_correct_arguments(env, content, name, args, kwargs):
    result = await commands.handle_command(content, "team", "admin")
    getattr(env.cleanup, name).assert_called_once_with(*args, **kwargs)
    assert "[internal]" not in result[0]["text"]


@pytest.mark.parametrize("content", [
    "/清理预览", "/清理预览 过期比赛 0天", "/清理预览 测试数据 30天", "/清理预览 未知", "/清理确认",
    "/标记测试 比赛 -1", "/标记测试 未知 3", "/取消测试标记 比赛 0", "/恢复比赛 A",
    "/清理规则 测试数据 0天 开启", "/清理规则 过期比赛 1天 重置",
])
async def test_invalid_arguments_rejected_without_backend_write(env, content):
    result = await commands.handle_command(content, "team", "admin")
    assert result[0]["error_code"] == "validation"
    for name in vars(env.cleanup):
        getattr(env.cleanup, name).assert_not_called()


async def test_expired_or_wrong_confirmation_displays_backend_reason(env):
    env.cleanup.apply_cleanup.side_effect = ValueError("确认码已过期，请重新预览。")
    result = await commands.handle_command("/清理确认 old-code", "team", "admin")
    assert "确认码已过期" in result[0]["text"]


async def test_backend_failure_returns_internal_error(env):
    env.cleanup.preview_cleanup.side_effect = RuntimeError("sqlite unavailable")
    result = await commands.handle_command("/清理预览 过期比赛", "team", "admin")
    assert result[0]["error_code"] == "internal"


async def test_private_event_missing_selection_returns_selection_prompt_without_fallback(env, monkeypatch):
    monkeypatch.setattr(main, "admin_group_id", Mock(side_effect=AssertionError("清理不得 fallback")))
    await env.bot.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id="admin", msg_id="inbound", content="/清理预览 过期比赛",
    ))
    text = env.api.post_c2c_message.call_args.kwargs["content"]
    assert "请先 /选群" in text
    env.cleanup.preview_cleanup.assert_not_called()
    env.api.post_group_message.assert_not_awaited()


async def test_private_confirmation_never_sends_group_message(env):
    admin_session.set_active_group("admin", "other")
    await env.bot.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id="admin", msg_id="inbound", content="/清理确认 confirm-code",
    ))
    env.cleanup.apply_cleanup.assert_called_once_with("other", "confirm-code", operator="admin")
    assert "本群清理完成" in env.api.post_c2c_message.call_args.kwargs["content"]
    env.api.post_group_message.assert_not_awaited()


def test_help_exposes_cleanup_by_role(env):
    assert "/清理预览" in commands.build_help("teacher", "清理")
    assert "/清理确认" not in commands.build_help("teacher", "清理")
    assert "/清理确认" in commands.build_help("admin", "清理")
    assert "/清理预览" not in commands.build_help("member", "清理")
