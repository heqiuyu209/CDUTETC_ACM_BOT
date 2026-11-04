"""帮助列表按现行命令权限展示，防止注册别名再次漏列。"""

import pytest

from cdutetc import commands, config


@pytest.mark.parametrize("member", ["teacher", "admin", "student", None])
def test_identity_list_aliases_follow_management_permissions(monkeypatch, member):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})

    text = commands.build_help(member)
    is_manager = member in {"teacher", "admin"}
    assert ("/群列表" in text) is is_manager
    assert ("/老师列表" in text) is is_manager
    assert "/帮助" in text
    if is_manager:
        assert text.count("/群列表") == 1
        assert text.count("/老师列表") == 1
