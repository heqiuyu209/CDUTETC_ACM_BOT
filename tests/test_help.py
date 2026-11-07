"""默认帮助简短，主题用法仍遵守命令权限。"""

import pytest

from cdutetc import commands, config


@pytest.mark.parametrize("member", ["teacher", "admin", "student", None])
def test_identity_list_aliases_follow_management_permissions(monkeypatch, member):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})

    compact = commands.build_help(member)
    assert len(compact.splitlines()) <= 9 and len(compact) <= 300
    assert "/帮助" in compact and "/加群" not in compact and "/修正" not in compact
    text = commands.build_help(member, "管理")
    is_manager = member in {"teacher", "admin"}
    assert ("/群列表" in text) is is_manager
    assert ("/老师列表" in text) is is_manager
    assert "/帮助" in text
    if is_manager:
        assert text.count("/群列表") == 1
        assert text.count("/老师列表") == 1


async def test_topic_argument_routes_to_examples_without_expanding_unrelated_commands(monkeypatch):
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    text = (await commands.handle_command("/help 比赛", "g1", "admin"))[0]["text"]
    assert "明天下午3点" in text and "/待确认列表" in text
    assert "/FAQ删除" not in text and "/命名群" not in text
    full = (await commands.handle_command("/帮助 全部", "g1", "admin"))[0]["text"]
    assert "/FAQ删除" in full and "/命名群" in full


def test_unknown_topic_returns_short_guidance():
    text = commands.build_help(None, "不知道怎么用")
    assert len(text.splitlines()) <= 2 and "/帮助 比赛" in text
