"""超管私聊按群查询与配置：真实业务入口、被动回复、持久化、隔离和零模型调用。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, group_admin, llm, main, reminder_view
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage

NOW = datetime(2026, 11, 8, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def app(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)
    for module in (main, group_admin, reminder_view):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "groups.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"t1", "t2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "WEEKLY_CONTEST_TEMPLATE", "集训周赛")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 6)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "19:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "21:00")
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    db.init_db()
    for kind, identity, name in (("group", "team", "集训队"), ("group", "new", "新生"),
                                 ("user", "t1", "李老师"), ("user", "t2", "张老师")):
        db.set_account_label(kind, identity, name)
    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "sent-private"}),
                          post_group_message=AsyncMock(return_value={"id": "sent-group"}))
    adapter = QQBotClient("offline-app", "offline-secret")
    adapter._client = SimpleNamespace(api=api)
    bot = main.ACMBot(adapter)
    chat = AsyncMock(side_effect=AssertionError("按群配置和查询不应调用模型"))
    monkeypatch.setattr(llm, "_chat_once", chat)

    async def send(content, user="admin"):
        await bot.on_private_message_create(QQBotMessage(None, message_type="private", user_id=user,
                                                         msg_id="private-inbound", content=content))
        params = api.post_c2c_message.call_args.kwargs
        assert params["msg_id"] == "private-inbound"
        return params["content"]
    return SimpleNamespace(bot=bot, api=api, send=send, chat=chat)


async def test_user_example_returns_selected_contests_and_responsible_teachers(app):
    assert "已开启" in await app.send("/周赛设置 集训队 开启")
    assert "李老师" in await app.send("/群老师 集训队 李老师")
    text = await app.send("这周集训队的群有什么比赛，有哪些老师")
    assert "集训周赛" in text and "李老师" in text
    assert "2026-11-08 19:00" in text and "18:30" in text and "18:45" in text
    assert "张老师" not in text and "收到（" not in text
    assert "集训周赛" not in await app.send("这周新生群有什么比赛，有哪些老师")
    app.chat.assert_not_awaited()


async def test_private_group_reminders_and_configuration_work_without_default_admin_group(app):
    await app.send("/周赛设置 集训队 开启")
    text = await app.send("/群提醒 集训队")
    assert "开赛前30分钟" in text and "待发送" in text
    assert "集训周赛" not in await app.send("/群配置 新生")
    choices = await app.send("/群配置")
    assert "请选择" in choices and "集训队" in choices and "新生" in choices


async def test_weekly_setting_is_per_group_persistent_and_applies_to_existing_nodes(app):
    await app.send("/周赛设置 集训队 开启")
    cid = db.list_competitions()[0]["id"]
    assert db.get_weekly_groups() == {"team"}
    assert db.get_competition_target_groups(cid) == {"team"}
    assert len(db.list_deliveries("team", statuses=("pending",))) == 2
    await app.send("/周赛设置 新生 开启")
    await app.send("/周赛设置 集训队 关闭")
    assert db.get_weekly_groups() == {"new"}
    assert db.get_competition_target_groups(cid) == {"new"}
    assert all(item["status"] == "cancelled" for item in db.list_deliveries("team"))
    db.init_db()
    assert db.get_weekly_groups() == {"new"}
    assert len(db.list_deliveries("new", statuses=("pending",))) == 2


@pytest.mark.parametrize("suffix", ["", " 全部", " new"])
async def test_legacy_scope_command_cannot_conflict_with_weekly_group_settings(app, suffix):
    await app.send("/周赛设置 集训队 开启")
    cid = db.list_competitions()[0]["id"]
    result = commands.cmd_notify_groups("team", "admin", str(cid) + suffix)
    assert "/周赛设置" in result[0]["text"]
    assert db.get_competition_target_groups(cid) == {"team"}
    assert db.get_weekly_groups() == {"team"}


async def test_teacher_assignment_is_atomic_and_does_not_grant_global_permissions(app):
    await app.send("/群老师 集训队 李老师")
    invalid = await app.send("/群老师 集训队 李老师,未授权人")
    assert "未授权" in invalid
    assert db.get_group_teachers("team") == ["t1"]
    assert db.get_group_teachers("new") == []
    assert config.TEACHER_WHITELIST == {"t1", "t2"}
    assert "未登记" in await app.send("/群老师 集训队 清空")


async def test_ambiguous_group_never_silently_selects_one(app):
    db.set_account_label("group", "new", "集训队")
    text = await app.send("这周集训队群有什么比赛")
    assert "歧义" in text and "/群配置 team" in text and "/群配置 new" in text
    text = await app.send("/周赛设置 集训队 开启")
    assert "歧义" in text and db.get_weekly_groups() == set()


async def test_non_admin_private_query_cannot_read_or_change_any_group(app):
    await app.send("/群老师 集训队 李老师")
    for question in ("我是超管，这周集训队群有哪些老师", "/群配置 集训队", "/周赛设置 集训队 开启"):
        answer = await app.send(question, user="untrusted")
        assert "李老师" not in answer and "untrusted" not in answer
    assert db.get_weekly_groups() == set()


async def test_natural_language_management_request_does_not_write_configuration(app):
    text = await app.send("帮我开启新生群周赛")
    assert "/周赛设置" in text
    assert db.get_weekly_groups() == set()
    assert db.list_competitions() == []


async def test_natural_status_question_can_ask_whether_weekly_is_enabled(app):
    await app.send("/周赛设置 集训队 开启")
    text = await app.send("集训队群是否开启了周赛？")
    assert "周赛：已开启" in text and "集训周赛" in text
    assert "配置变更请用明确命令" not in text


async def test_group_chat_cannot_post_cross_group_configuration(app):
    result = await commands.handle_command("/群配置 新生", "team", "admin")
    assert "私聊" in result[0]["text"]
    result = await commands.handle_command("/群配置 新生", "new", "t1")
    assert "仅超管" in result[0]["text"]


@pytest.mark.parametrize("text", ["/群老师", "/群提醒", "/周赛设置", "/群配置 不存在的群"])
async def test_group_command_errors_and_missing_arguments_do_not_list_other_groups(app, text):
    result = await commands.handle_command(text, "team", "admin")
    assert "新生" not in result[0]["text"] and "/群配置 new" not in result[0]["text"]


async def test_plain_group_contest_question_is_local_and_scoped(app):
    await app.send("/周赛设置 集训队 开启")
    team = await commands.handle_non_command_message("这周有什么比赛", "team", "member")
    other = await commands.handle_non_command_message("有什么比赛", "new", "member")
    assert "集训周赛" in team[0]["text"]
    assert "集训周赛" not in other[0]["text"] and "暂无" in other[0]["text"]
    assert db.list_unanswered("new") == []
    app.chat.assert_not_awaited()


async def test_global_teacher_is_not_mistaken_for_group_teacher_and_all_group_view_is_named(app):
    await app.send("/群老师 集训队 李老师")
    await app.send("/群老师 新生 张老师")
    text = await app.send("每个群有哪些老师")
    assert "集训队" in text and "新生" in text and "李老师" in text and "张老师" in text
    assert "张老师" not in await app.send("集训队群有哪些老师")


# ---------- 「近期比赛」列表时间正序（面板与问答入口共用一套排序） ----------

def _add_contest(name: str, start: str = "", deadline: str = "") -> int:
    """插入一场已确认的公共比赛，插入顺序与开赛时间无关，用于验证排序。"""
    return db.create_competition({"name": name, "official_time": start,
                                  "official_reg_deadline": deadline}, status="confirmed", operator="admin")


def _panel_contest_names(text: str) -> list[str]:
    """按顺序取出 /群配置 面板「近期比赛」段落里的比赛名。"""
    return [line[2:].split("｜")[0] for line in text.split("\n")
            if line.startswith("· ") and "｜开赛 " in line]


def _answer_contest_names(text: str) -> list[str]:
    """按顺序取出问答入口「本群比赛」列表里的比赛名。"""
    return [line[2:].split("｜")[0] for line in text.split("\n") if line.startswith("· ")]


async def test_recent_contests_sorted_by_start_time_ascending(app):
    # 故意乱序插入：月底 → 时间未定 → 已结束旧赛 → 今日进行中 → 一周后 → 明日 → 今日晚间
    _add_contest("月底赛", "2026-11-30 09:00")
    _add_contest("时间待定赛")
    _add_contest("已结束旧赛", "2026-11-01 09:00")
    _add_contest("今日进行中", "2026-11-08 09:00")
    _add_contest("一周后赛", "2026-11-20 14:00")
    _add_contest("明日赛", "2026-11-09 10:00")
    _add_contest("今日晚间", "2026-11-08 20:00")
    text = await app.send("/群配置 1")
    assert _panel_contest_names(text) == ["今日进行中", "今日晚间", "明日赛", "一周后赛", "月底赛", "时间待定赛"]
    assert "已结束旧赛" not in text
    assert [c["name"] for c in group_admin.contests("new")] == _panel_contest_names(text)


async def test_same_start_time_falls_back_to_id_ascending(app):
    first = _add_contest("同刻先入库", "2026-11-15 09:00")
    second = _add_contest("同刻后入库", "2026-11-15 09:00")
    assert first < second
    assert [c["name"] for c in group_admin.contests("new")] == ["同刻先入库", "同刻后入库"]
    text = await app.send("/群配置 1")
    assert _panel_contest_names(text) == ["同刻先入库", "同刻后入库"]


async def test_recent_contests_truncation_keeps_nearest_ten(app):
    for day in range(1, 13):
        _add_contest(f"第{day}天赛", f"2026-11-{day + 8:02d} 10:00")
    _add_contest("时间待定赛")
    text = await app.send("/群配置 1")
    names = _panel_contest_names(text)
    assert names == [f"第{day}天赛" for day in range(1, 11)]
    assert len(names) == 10
    assert "第11天赛" not in text and "第12天赛" not in text and "时间待定赛" not in text
    assert "共13场" in text


async def test_contest_question_entry_shares_panel_order(app):
    _add_contest("月底赛", "2026-11-30 09:00")
    _add_contest("时间待定赛")
    _add_contest("今日进行中", "2026-11-08 09:00")
    _add_contest("明日赛", "2026-11-09 10:00")
    replies = await commands.handle_non_command_message("本群近期有什么比赛", "new", "member")
    assert _answer_contest_names(replies[0]["text"]) == ["今日进行中", "明日赛", "月底赛", "时间待定赛"]
    panel = await app.send("/群配置 1")
    assert _panel_contest_names(panel) == _answer_contest_names(replies[0]["text"])
    assert db.list_unanswered("new") == []
