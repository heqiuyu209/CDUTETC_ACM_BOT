"""超管命令交互简化：工作区持久化、免 group_openid、短别名、免引号改期、/管理 面板。

覆盖验收点：
1) 工作区落库 + 进程重启后从库恢复；
2) 未选定工作区时默认绑定正式群（名称含「集训队」），不落到测试群；
3) /切换 支持群名关键字与序号；
4) /绑老师 <姓名> 省略群标识即作用于当前工作区；
5) /改 <id> 11-29 09:00 免引号改期，并重排开赛提醒；
6) /管理 一屏总览（工作区 / 待确认发布 / 提醒 / 短命令速查）。
"""

import asyncio
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, commands, config, group_admin
from cdutetc import database as db

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=TZ)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "workspace.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(commands, "datetime", Clock)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ADMIN_GROUP", "team")
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"孔老师", "t1"})
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())

    admin_session.reset_all()
    db.init_db()
    db.set_account_label("group", "team", "集训队")
    db.set_account_label("group", "new", "新生群")
    yield SimpleNamespace()
    admin_session.reset_all()


def _contest_plans(cid: int):
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute(
            "SELECT target_time, scheduled_at FROM reminder_deliveries"
            " WHERE obj_type = 'competition' AND obj_id = ? AND remind_type = 'contest_start'"
            " AND status = 'pending' ORDER BY scheduled_at", (cid,),
        ).fetchall()


def _new_competition(name: str, official_time: str, group: str = "team") -> int:
    pid = db.create_pending_publication(
        {"name": name, "official_time": official_time}, group, "", "", "teacher",
    )
    cid, _ = db.approve_pending_publication(pid, "admin")
    return cid


def test_workspace_persists_and_restores_after_restart(env):
    admin_session.set_active_group("admin", "new")
    assert db.get_admin_workspace("admin") == "new"

    # 模拟进程重启：内存缓存清空，绑定关系应从 contests.db 恢复
    admin_session.reset_all()
    assert admin_session._ACTIVE_GROUPS == {}
    assert admin_session.get_active_group("admin") == "new"
    assert group_admin.active_workspace("admin") == "new"


def test_explicit_clear_survives_restart(env):
    admin_session.set_active_group("admin", "new")
    admin_session.clear_active_group("admin")
    admin_session.reset_all()
    # 显式清空后不再自动回落到默认群
    assert admin_session.get_active_group("admin") is None
    assert group_admin.active_workspace("admin") == ""


def test_default_workspace_prefers_formal_group(env, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "sandbox"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "sandbox"})
    db.set_account_label("group", "sandbox", "调试测试群")

    assert group_admin.default_workspace() == "team"
    # 从未选定时私聊自动落到正式群，且不写库（保持「无记录」语义）
    assert group_admin.active_workspace("admin") == "team"
    assert db.has_admin_workspace("admin") is False


def test_switch_workspace_by_keyword_and_index(env):
    out = commands.cmd_switch_workspace("admin", "新生")
    assert "已将操作群切换为" in out[0]["text"]
    assert admin_session.get_active_group("admin") == "new"

    # 序号按群号排序：1 = new，2 = team
    commands.cmd_switch_workspace("admin", "2")
    assert admin_session.get_active_group("admin") == "team"

    # 切换同时落库
    assert db.get_admin_workspace("admin") == "team"


def test_alias_names_map_to_official_commands(env):
    assert commands.canonical_command("改") == "修正"
    assert commands.canonical_command("绑老师") == "群老师"
    assert commands.canonical_command("面板") == "管理"


def test_bind_teacher_without_group_argument(env):
    admin_session.set_active_group("admin", "team")
    out = asyncio.run(commands.handle_command("/绑老师 孔老师", "new", "admin", private=True))
    assert "负责老师" in out[0]["text"]
    # 省略群标识时落到工作区 team，而不是回执所在群 new
    assert db.get_group_teachers("team") == ["孔老师"]
    assert db.get_group_teachers("new") == []


def test_group_admin_accepts_short_group_keyword(env):
    out = group_admin.handle_command("群老师", "新生 孔老师", "admin", "", private=True)
    assert "负责老师" in out[0]["text"]
    assert db.get_group_teachers("new") == ["孔老师"]


def test_inline_amend_time_updates_reminders(env):
    cid = _new_competition("蓝桥杯", "2026-11-20 09:00")
    assert "2026-11-20 09:00" in {row[0] for row in _contest_plans(cid)}

    out = asyncio.run(commands.cmd_amend("team", "admin", f"{cid} 11-29 09:00"))
    assert f"#{cid}" in out[0]["text"]
    assert "2026-11-29 09:00" in out[0]["text"]
    assert db.get_competition(cid)["official_time"] == "2026-11-29 09:00"

    # 开赛提醒按新时间重排，旧节点不再保留
    targets = {row[0] for row in _contest_plans(cid)}
    assert "2026-11-29 09:00" in targets
    assert "2026-11-20 09:00" not in targets


def test_inline_amend_url_and_name_are_recognised(env):
    cid = _new_competition("蓝桥杯", "2026-11-20 09:00")

    out = asyncio.run(commands.cmd_amend("team", "admin", f"{cid} https://example.com/icpc"))
    assert "official_site=https://example.com/icpc" in out[0]["text"]
    assert db.get_competition(cid)["official_site"] == "https://example.com/icpc"

    out = asyncio.run(commands.cmd_amend("team", "admin", f"{cid} 四川省大学生程序设计竞赛"))
    assert "四川省大学生程序设计竞赛" in out[0]["text"]
    assert db.get_competition(cid)["name"] == "四川省大学生程序设计竞赛"


def test_inline_amend_keeps_legacy_key_value_syntax(env):
    cid = _new_competition("蓝桥杯", "2026-11-20 09:00")
    out = asyncio.run(commands.cmd_amend("team", "admin", f'{cid} name="蓝桥杯省赛"'))
    assert "蓝桥杯省赛" in out[0]["text"]
    assert db.get_competition(cid)["name"] == "蓝桥杯省赛"


def test_admin_panel_summarises_workspace_pending_and_shortcuts(env):
    admin_session.set_active_group("admin", "new")
    db.create_pending_publication(
        {"name": "新生赛", "official_time": "2026-11-20 09:00"}, "new", "", "", "teacher",
    )

    out = commands.cmd_admin_panel("new", "admin", "", gid="new")
    text = out[0]["text"]
    assert "工作区" in text and "新生群" in text
    assert "待确认发布：1 条" in text and "#1" in text
    assert "/绑老师" in text and "/改 <id> 11-29 09:00" in text


def test_admin_panel_requires_super_admin(env):
    out = commands.cmd_admin_panel("team", "t1", "", gid="team")
    assert "仅超管" in out[0]["text"]
