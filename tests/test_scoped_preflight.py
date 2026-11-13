"""超管/老师分级作用域与比赛主动发信预检：作用域矩阵、闸门顺序、诊断渲染与入口拦截。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, group_admin, llm, main, reminder_preflight, roles
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage

NOW = datetime(2026, 11, 11, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)
    for module in (main, reminder_preflight):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "scope.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"t1", "t2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "WEEKLY_CONTEST_TEMPLATE", "集训周赛")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 6)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "19:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "21:00")
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    db.init_db()
    for kind, identity, name in (("group", "team", "集训队"), ("group", "new", "新生")):
        db.set_account_label(kind, identity, name)
    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "sent-private"}),
                          post_group_message=AsyncMock(return_value={"id": "sent-group"}))
    adapter = QQBotClient("offline-app", "offline-secret")
    adapter._client = SimpleNamespace(api=api)
    bot = main.ACMBot(adapter)
    chat = AsyncMock(side_effect=AssertionError("只读诊断不应调用模型"))
    monkeypatch.setattr(llm, "_chat_once", chat)

    async def send_private(content, user="admin"):
        await bot.on_private_message_create(QQBotMessage(None, message_type="private", user_id=user,
                                                         msg_id="private-inbound", content=content))
        return api.post_c2c_message.call_args.kwargs["content"]
    return SimpleNamespace(api=api, send_private=send_private, chat=chat)


def _contest(name="Codeforces Round 990", official_time="2026-11-15 22:00",
             groups=("team",), status="confirmed") -> int:
    cid = db.create_competition({"name": name, "official_time": official_time}, status=status)
    db.set_competition_notify_groups(cid, list(groups))
    return cid


def _schedule(group_id: str, cid: int, target_time="2026-11-15 22:00", offset_hours=0.5) -> None:
    db.ensure_delivery(group_id, "competition", cid, "contest_start", target_time, offset_hours)


def _cancel_nodes(group_id: str, cid: int) -> None:
    """模拟节点已被取消：只剩「查得到但到点不推」的比赛。"""
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET status='cancelled'"
                     " WHERE group_id=? AND obj_type='competition' AND obj_id=?",
                     (group_id, cid))


# --------------------------------------------------------------- 模块 A：作用域矩阵

@pytest.mark.parametrize("user, group_id, expected", [
    ("admin", None, "global"),
    ("admin", "team", "group"),
    ("t1", "team", "group"),
    ("t1", None, None),
    ("member", "team", None),
    ("admin", "unknown", None),
    ("t1", "unknown", None),
])
def test_resolve_admin_scope_matrix(env, user, group_id, expected):
    assert roles.resolve_admin_scope(user, group_id) == expected


def test_group_registered_teacher_list_narrows_scope(env):
    db.set_group_teachers("team", ["t1"])
    assert roles.resolve_admin_scope("t1", "team") == "group"
    assert roles.resolve_admin_scope("t2", "team") is None


# --------------------------------------------------------------- 模块 B：预检引擎

def test_ready_contest_reports_expected_reminder_time(env):
    cid = _contest()
    _schedule("team", cid)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert ok and message == "就绪 (预计提醒时间: 2026-11-15 21:30)"


def test_missing_delivery_node_blocks(env):
    cid = _contest()
    _cancel_nodes("team", cid)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and message.startswith("阻断原因:") and "无待触发" in message


def test_past_delivery_node_does_not_count_as_ready(env):
    cid = _contest()
    _cancel_nodes("team", cid)
    _schedule("team", cid, target_time="2026-11-10 22:00")
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and "无待触发" in message


def test_unbound_group_is_blocked_with_actionable_reason(env):
    cid = _contest(groups=("new",))
    _schedule("new", cid)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and "/通知群" in message


def test_unauthorized_group_is_blocked_by_whitelist(env):
    cid = _contest()
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "ghost")
    assert not ok and "白名单" in message


def test_global_active_switch_off_blocks_even_when_nodes_exist(env, monkeypatch):
    cid = _contest()
    _schedule("team", cid)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and "总开关" in message


def test_quota_fuse_blocks_even_when_nodes_exist(env):
    cid = _contest()
    _schedule("team", cid)
    db.block_group_notifications("qqbot", "team", "QUOTA_EXHAUSTED", 11253)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and "熔断" in message and "11253" in message


def test_draft_contest_is_not_ready(env):
    cid = _contest(status="draft")
    _schedule("team", cid)
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert not ok and "尚未确认" in message


def test_unknown_contest_reports_missing(env):
    ok, message = reminder_preflight.check_contest_delivery_readiness(999, "team")
    assert not ok and "不存在" in message


def test_weekly_contest_is_ready_only_for_enabled_groups(env, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", True)
    group_admin.handle_command("周赛设置", "team 开启", "admin")
    assert db.get_weekly_groups() == {"team"}
    cid, created = db.create_weekly_competition(
        "2026-11-09", {"name": "集训周赛", "official_time": "2026-11-15 19:00"})
    assert created and db.is_weekly_competition(cid)
    assert db.get_competition_target_groups(cid) == {"team"}
    _schedule("team", cid, target_time="2026-11-15 19:00")
    ok, message = reminder_preflight.check_contest_delivery_readiness(cid, "team")
    assert ok and message.startswith("就绪 (预计提醒时间: ")
    blocked, reason = reminder_preflight.check_contest_delivery_readiness(cid, "new")
    assert not blocked and "ACM_WEEKLY_CONTEST_GROUPS" in reason


# --------------------------------------------------------------- 模块 C：诊断视图

def test_diagnosis_separates_ready_from_blocked(env):
    ready_id = _contest()
    _schedule("team", ready_id)
    blocked_id = _contest(name="校外公开邀请赛")
    _cancel_nodes("team", blocked_id)
    text = reminder_preflight.render_diagnosis(["team"])
    assert "【CDUTETC ACM 比赛与提醒状态诊断】" in text
    assert "主动发信: 开启" in text and "熔断状态: 正常" in text
    assert "🟢【主动提醒就绪】" in text and f"[ID: {ready_id}] Codeforces Round 990" in text
    assert "预计提醒: 2026-11-15 21:30 (赛前 30 分钟)" in text
    assert "🟡【已收录但不可主动提醒】" in text and f"[ID: {blocked_id}]" in text
    assert "⚠️ 阻断原因:" in text
    assert "只有标记为【主动提醒就绪】的比赛到点才会由机器人主动推送。" in text


def test_diagnosis_shows_fuse_state_and_global_scope_summary(env):
    cid = _contest()
    _schedule("team", cid)
    db.block_group_notifications("qqbot", "new", "QUOTA_EXHAUSTED", 11263)
    text = reminder_preflight.render_diagnosis(["team", "new"])
    assert "共 2 个已授权群" in text
    assert "熔断状态: 暂停" in text and "11263" in text


def test_command_scope_denied_for_plain_member_in_group(env):
    replies = commands.cmd_contest_status("team", "member", "")
    assert replies[0]["text"] == "⚠️ 该命令仅限本群老师或超级管理员执行"


def test_command_scope_denied_for_teacher_private(env):
    replies = commands.cmd_contest_status("team", "t1", "", private=True)
    assert replies[0]["text"] == "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令"


def test_group_command_only_shows_current_group(env):
    team_id = _contest()
    _schedule("team", team_id)
    new_id = _contest(name="新生公开赛", groups=("new",))
    _schedule("new", new_id)
    text = commands.cmd_contest_status("team", "t1", "")[0]["text"]
    assert f"[ID: {team_id}]" in text and f"[ID: {new_id}]" not in text


async def test_private_command_for_super_admin_covers_all_groups(env):
    team_id = _contest()
    _schedule("team", team_id)
    new_id = _contest(name="新生公开赛", groups=("new",))
    _schedule("new", new_id)
    text = commands.cmd_contest_status("team", "admin", "", private=True)[0]["text"]
    assert f"[ID: {team_id}]" in text and f"[ID: {new_id}]" in text and "共 2 个已授权群" in text


async def test_teacher_private_message_is_blocked_at_entry_without_llm(env):
    text = await env.send_private("/比赛状态", user="t1")
    assert text == "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令"
    env.chat.assert_not_awaited()


async def test_plain_member_private_message_is_blocked_at_entry(env):
    text = await env.send_private("/群配置 集训队", user="member")
    assert "私聊管理仅对超级管理员开放" in text
    env.chat.assert_not_awaited()


async def test_non_command_private_message_gets_friendly_redirect(env):
    text = await env.send_private("这周有比赛吗", user="member")
    assert text == "你好～群内问题请在群里 @我，私聊管理仅向超管开放。"
    env.chat.assert_not_awaited()
