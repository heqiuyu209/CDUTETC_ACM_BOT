"""默认帮助简短，主题用法仍遵守命令权限；超管帮助只描述改版后的短命令。"""

import pytest

from cdutetc import commands, config

# 超管改版后的短命令与旧长命令/占位符，帮助文案必须只留前者
_SHORT_COMMANDS = ("/管理", "/切换", "/绑老师", "/改", "/待确认")
# 裸 openid 现为 /加老师 三段式的合法第一段参数，帮助文案须如实展示，不再视为旧占位符；
# 仍禁止暴露 group_openid 这类内部群标识与旧的长命令占位符。
_LEGACY_HINTS = ("group_openid", "<群标识>", "/群配置 <", "/选群 [")


@pytest.mark.parametrize("member", ["teacher", "admin", "student", None])
def test_identity_list_aliases_follow_management_permissions(monkeypatch, member):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})

    compact = commands.build_help(member)
    assert len(compact.splitlines()) <= 9 and len(compact) <= 300
    assert "/帮助" in compact and "/加群" not in compact and "/修正" not in compact
    # 普通成员与群内展示都看不到超管快捷命令区
    assert "【超管快捷命令】" not in compact
    text = commands.build_help(member, "管理")
    is_manager = member in {"teacher", "admin"}
    assert ("/群列表" in text) is is_manager
    assert ("/老师列表" in text) is is_manager
    assert "/帮助" in text
    if is_manager:
        assert text.count("/群列表") == 1
        assert text.count("/老师列表") == 1
    else:
        # 普通队员不暴露任何超管命令
        for cmd in _SHORT_COMMANDS:
            assert cmd not in text


def test_member_help_never_exposes_super_admin_commands(monkeypatch):
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    for member in ("student", None):
        full = commands.build_help(member, "全部")
        for cmd in _SHORT_COMMANDS:
            assert cmd not in full


def test_super_admin_private_help_shows_short_command_block(monkeypatch):
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    private_help = commands.build_help("admin", private=True)
    assert "【超管快捷命令】" in private_help
    for snippet in ("/管理（也可 /面板）", "/切换 集训队", "/绑老师 老师姓名",
                    "/改 编号 11-29 09:00", "/改 编号 官网链接",
                    "私聊发比赛通知（或 /待确认）", "同意 编号 / 拒绝 编号"):
        assert snippet in private_help
    # 群内（非私聊）不展开超管命令，只给一句引导
    group_help = commands.build_help("admin")
    assert "【超管快捷命令】" not in group_help
    assert "私聊发 /帮助" in group_help


def test_management_topic_uses_short_commands_only(monkeypatch):
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    text = commands.build_help("admin", "管理")
    for snippet in ("/管理 →", "/切换 <序号|群名>", "/绑老师 老师姓名",
                    "/改 <编号> <时间|链接|名称|赛道>", "/待确认",
                    "/重置群", "/晨报预览"):
        assert snippet in text
    for legacy in _LEGACY_HINTS:
        assert legacy not in text
    assert "/待确认列表" not in text.replace("（旧写法 /待确认列表）", "")


async def test_topic_argument_routes_to_examples_without_expanding_unrelated_commands(monkeypatch):
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    text = (await commands.handle_command("/help 比赛", "g1", "admin"))[0]["text"]
    assert "明天下午3点" in text and "/待确认" in text
    assert "同意 编号 / 拒绝 编号" in text
    assert "group_openid" not in text
    assert "/FAQ删除" not in text and "/命名群" not in text
    full = (await commands.handle_command("/帮助 全部", "g1", "admin"))[0]["text"]
    assert "/FAQ删除" in full and "/命名群" in full


async def test_private_help_passes_private_flag(monkeypatch):
    """handle_command 私聊分支要把 private 透传给 build_help，才能展开超管快捷区。"""
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    private_text = (await commands.handle_command(
        "/帮助", "g1", "admin", private=True))[0]["text"]
    assert "【超管快捷命令】" in private_text
    group_text = (await commands.handle_command(
        "/帮助", "g1", "admin", private=False))[0]["text"]
    assert "【超管快捷命令】" not in group_text


def test_unknown_topic_returns_short_guidance():
    text = commands.build_help(None, "不知道怎么用")
    assert len(text.splitlines()) <= 2 and "/帮助 比赛" in text


def test_pending_alias_routes_to_publish_approval_list(monkeypatch):
    """/待确认 是 /待确认列表 的短别名，帮助里推荐的就是它。"""
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    assert commands.canonical_command("待确认") == "待确认列表"
    assert commands.canonical_command("面板") == "管理"
    assert commands.canonical_command("绑老师") == "群老师"
    assert commands.canonical_command("改") == "修正"
