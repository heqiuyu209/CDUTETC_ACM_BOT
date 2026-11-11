"""entity_aliases 别名解析与统一渲染的单元测试。

覆盖三件事：
1. 别名表读写（重复绑定覆盖、类型校验、删除）；
2. 未认领时降级成短哈希，禁止裸露长 openid；
3. /认领此群、/认领老师 的落库 + 白名单/老师表联动，以及群列表与配置提示渲染。
"""

import asyncio
import json
import sqlite3

import pytest

from cdutetc import alias_helper, commands, config, database, group_admin


@pytest.fixture()
def memory_db(monkeypatch):
    """每个用例一套独立内存库，避免污染真实 contests.db。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    database.init_db()
    yield conn
    conn.close()


GROUP = "A0EF5D2D8BA4B3CC76E3C70C47971FDE"
OTHER = "B1C2D3E4F5A6B7C8D9E0F1A2B3C4D5E6"
USER = "DCF4F26867BD71753336F962C626F3FD"


# ---------------------------------------------------------------- 别名读写
def test_alias_roundtrip(memory_db):
    assert database.get_entity_alias("group", GROUP) is None
    assert database.set_entity_alias("group", GROUP, "集训队群", "123456789")
    row = database.get_entity_alias("group", GROUP)
    assert row["display_name"] == "集训队群"
    assert row["numeric_id"] == "123456789"
    assert row["entity_type"] == "group"
    assert row["updated_at"]


def test_alias_rebind_overwrites_and_lists(memory_db):
    database.set_entity_alias("group", GROUP, "旧名字", "111")
    database.set_entity_alias("group", GROUP, "新名字", "222")
    aliases = database.list_group_aliases()
    assert list(aliases) == [GROUP]
    assert aliases[GROUP]["display_name"] == "新名字"
    assert aliases[GROUP]["numeric_id"] == "222"


def test_alias_isolated_between_group_and_user(memory_db):
    database.set_entity_alias("group", "x1", "群名", "1")
    database.set_entity_alias("user", "x1", "人名", "2")
    assert database.get_entity_alias("group", "x1")["display_name"] == "群名"
    assert database.get_entity_alias("user", "x1")["display_name"] == "人名"
    assert list(database.list_group_aliases()) == ["x1"]


def test_alias_rejects_bad_input(memory_db):
    with pytest.raises(ValueError):
        database.set_entity_alias("team", GROUP, "名字")
    with pytest.raises(ValueError):
        database.set_entity_alias("group", "", "名字")
    with pytest.raises(ValueError):
        database.set_entity_alias("group", GROUP, "   ")


def test_remove_alias(memory_db):
    database.set_entity_alias("user", USER, "张老师", "10001")
    assert database.remove_entity_alias("user", USER)
    assert database.get_entity_alias("user", USER) is None
    assert database.remove_entity_alias("user", USER) is False


# ------------------------------------------------------------ 降级与格式
def test_format_unclaimed_degrades_to_short_hash(memory_db):
    assert alias_helper.format_group_name(GROUP) == "未命名群[A0EF5D...1FDE]"
    assert alias_helper.format_user_name(USER) == "成员[DCF4F2...F3FD]"
    assert "A0EF5D2D8BA4B3CC76E3C70C47971FDE" not in alias_helper.format_group_name(GROUP)


def test_format_claimed_with_and_without_numeric(memory_db):
    database.set_entity_alias("group", GROUP, "集训队群", "123456789")
    assert alias_helper.format_group_name(GROUP) == "集训队群 (123456789)"
    database.set_entity_alias("group", GROUP, "集训队群")
    assert alias_helper.format_group_name(GROUP) == "集训队群"

    database.set_entity_alias("user", USER, "张老师", "10001")
    assert alias_helper.format_user_name(USER) == "张老师 (QQ: 10001)"
    database.set_entity_alias("user", USER, "张老师")
    assert alias_helper.format_user_name(USER) == "张老师"


def test_format_empty_and_legacy_label_fallback(memory_db):
    assert alias_helper.format_group_name("") == "未命名群"
    assert alias_helper.format_user_name("") == "成员"
    # 只走过 /命名群 的历史数据也要能显示名字
    database.set_account_label("group", GROUP, "历史群名")
    assert alias_helper.format_group_name(GROUP) == "历史群名"


def test_short_openid_keeps_short_values(memory_db):
    assert alias_helper.short_openid("abc") == "abc"
    assert alias_helper.short_openid("") == ""


# ------------------------------------------------------------ 群内认领命令
def test_claim_group_binds_alias_and_whitelist(memory_db):
    result = asyncio.run(commands.handle_command("/认领此群 123456789 集训队群", GROUP, "admin"))
    text = result[0]["text"]
    assert "已认领本群" in text
    assert "集训队群 (123456789)" in text
    assert database.get_entity_alias("group", GROUP)["numeric_id"] == "123456789"
    assert database.get_account_label("group", GROUP) == "集训队群"
    assert GROUP in config.ALLOWED_GROUPS


def test_claim_group_rejects_non_admin_and_bad_args(memory_db):
    text = asyncio.run(commands.handle_command("/认领此群 123 某群", GROUP, "member"))[0]["text"]
    assert "仅超管" in text
    text = asyncio.run(commands.handle_command("/认领此群", GROUP, "admin"))[0]["text"]
    assert "用法" in text
    text = asyncio.run(commands.handle_command("/认领此群 abc 某群", GROUP, "admin"))[0]["text"]
    assert "纯数字" in text
    assert database.get_entity_alias("group", GROUP) is None


def test_claim_teacher_binds_sender(memory_db):
    result = asyncio.run(commands.handle_command("/认领老师 10001 张老师", GROUP, "admin"))
    text = result[0]["text"]
    assert "已认领老师身份" in text
    assert "张老师 (QQ: 10001)" in text
    assert database.get_entity_alias("user", "admin")["display_name"] == "张老师"
    assert "admin" in config.TEACHER_WHITELIST


def test_claim_teacher_requires_privilege(memory_db):
    text = asyncio.run(commands.handle_command("/认领老师 10001 张老师", GROUP, "member"))[0]["text"]
    assert "仅超管或已授权老师" in text
    assert database.get_entity_alias("user", "member") is None


# -------------------------------------------------------- 群列表与配置提示
def test_group_picker_uses_formatted_names(memory_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GROUP})
    database.set_entity_alias("group", GROUP, "集训队群", "123456789")
    text = commands.render_group_picker(current=GROUP)
    assert "1. 集训队群 (123456789)" in text
    assert "✅ 当前" in text
    assert GROUP not in text


def test_group_admin_choices_and_index_selector(memory_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GROUP, OTHER})
    database.set_entity_alias("group", GROUP, "集训队群", "123456789")
    names = group_admin._choices()
    assert "1. " in names and "2. " in names
    assert "/群配置 " in names
    assert "集训队群 (123456789)" in names
    ordered = sorted(config.ALLOWED_GROUPS)
    assert group_admin.resolve_group("1") == ordered[0]
    assert group_admin.resolve_group("2") == ordered[1]
    with pytest.raises(ValueError):
        group_admin.resolve_group("99")


def test_teacher_names_never_leak_openid(memory_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GROUP})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {USER})
    database.set_entity_alias("user", USER, "张老师", "10001")
    assert group_admin._teacher_names(GROUP) == "未登记"
    database.set_group_teachers(GROUP, [USER])
    assert group_admin._teacher_names(GROUP) == "张老师 (QQ: 10001)"


# ------------------------------------------------------ 待确认发布渲染
def test_publish_review_uses_claimed_names(memory_db):
    from cdutetc import main

    database.set_entity_alias("group", GROUP, "集训队群", "123456789")
    database.set_entity_alias("user", USER, "张老师", "10001")
    pending = {
        "id": 7,
        "group_id": GROUP,
        "submitted_by": USER,
        "fields_json": json.dumps({"name": "蓝桥杯", "official_time": "2026-12-01 09:00"}),
    }
    text = main._build_publish_review(pending)
    assert "来源群：集训队群 (123456789)" in text
    assert "提交人：张老师 (QQ: 10001)" in text
    assert GROUP not in text and USER not in text


def test_publish_review_degrades_when_unclaimed(memory_db):
    from cdutetc import main

    pending = {"id": 8, "group_id": GROUP, "submitted_by": USER,
               "fields_json": json.dumps({"name": "蓝桥杯"})}
    text = main._build_publish_review(pending)
    assert "来源群：未命名群[A0EF5D...1FDE]" in text
    assert "提交人：成员[DCF4F2...F3FD]" in text
    assert GROUP not in text and USER not in text
