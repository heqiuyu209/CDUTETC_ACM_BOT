"""规格 C：/课程修改 的「位置参数 + 键值对」双模式，以及列表序号与工作区标牌的口径。

覆盖点：
1) 目标解析：序号 / #id / 课程名（含带空格课程名的最长回退）；
2) 时间解析：位置时间（周四 19:00-21:00 / 周四 19-21点 / 19点半-21点）与 时间= 键值对；
3) 位置时间之后还跟着标题时，标题与时间同时更新；关键词键值对优先于位置参数；
4) 列表只展示 1-based 序号，不再出现 #id；
5) 私聊标牌优先展示认领到的真实群号，逐级降级。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, commands, config, database as db

TZ = ZoneInfo("Asia/Shanghai")
GID = "123456"
TEACHER = "654321"
OUTSIDER = "999999"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离库 + 白名单：老师 TEACHER 可管课，OUTSIDER 为普通成员。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "modify_args.sqlite3"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "modify_args.sqlite3"), raising=False)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GID}, raising=False)
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {TEACHER}, raising=False)
    monkeypatch.setattr(config, "SUPER_ADMIN", {"root"}, raising=False)
    db.init_db()
    admin_session.reset_all()
    yield GID
    admin_session.reset_all()


def _text(result: list[dict]) -> str:
    return "\n".join(item["text"] for item in result)


def _add(title: str, extra: str = "周四 19-21点 二教B201") -> None:
    assert "已添加课程" in _text(commands.cmd_training_add(GID, TEACHER, f"{title} {extra}"))


# ---------------------------------------------------------------- 时间抽取单元

def test_extract_time_span_recognizes_four_positional_forms():
    """四种位置时间写法都能抽成 weekday/start/end，且不残留时间文本。"""
    assert commands._extract_time_span("周四 19:00-21:00") == (
        {"weekday": "周四", "start_time": "19:00", "end_time": "21:00"}, "")
    assert commands._extract_time_span("周四 19-21点") == (
        {"weekday": "周四", "start_time": "19:00", "end_time": "21:00"}, "")
    assert commands._extract_time_span("19:00-21:00") == (
        {"start_time": "19:00", "end_time": "21:00"}, "")
    assert commands._extract_time_span("19点半-21点") == (
        {"start_time": "19:30", "end_time": "21:00"}, "")


def test_extract_time_span_keeps_plain_range_as_title():
    """`3-5人` 这类没有「点/冒号/周几」的区间不当时间，避免把标题吞掉。"""
    fields, leftover = commands._extract_time_span("小组 3-5人 讨论")
    assert fields == {}
    assert leftover == "小组 3-5人 讨论"


# ---------------------------------------------------------------- 位置参数模式

def test_modify_by_index_with_positional_span(env):
    """序号 + 位置时间：只改时间，标题保持不变。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "1 周四 19:00-21:00"))
    assert "已更新课程「算法特训」" in text and "19:00-21:00" in text
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周四", "19:00", "21:00")
    assert course["title"] == "算法特训"


def test_modify_by_title_with_positional_span(env):
    """课程名 + 位置时间：`周四 19-21点` 归一化后落库。"""
    _add("图论进阶")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "图论进阶 周五 19-21点"))
    assert "已更新课程" in text
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"]) == ("周五", "19:00")


def test_modify_positional_span_with_trailing_requirement(env):
    """位置时间后面还跟着文本时，时间更新、尾随文本写入「要求」，标题保持不动。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "1 周四 19:00-21:00 双指针专题"))
    assert "时间=周四 19:00-21:00" in text
    assert "要求=双指针专题" in text
    course = db.get_group_course(GID, 1)
    assert course["title"] == "算法特训"
    assert course["requirement"] == "双指针专题"
    assert (course["start_time"], course["end_time"]) == ("19:00", "21:00")


def test_modify_without_time_puts_whole_tail_into_requirement(env):
    """没写时间时，整段尾部写入「要求」，标题与时间字段都保持原值。"""
    _add("算法特训")
    _text(commands.cmd_training_modify(GID, TEACHER, "1 期末复习与模拟赛安排"))
    course = db.get_group_course(GID, 1)
    assert course["title"] == "算法特训"
    assert course["requirement"] == "期末复习与模拟赛安排"
    assert (course["weekday"], course["start_time"]) == ("周四", "19:00")


def test_modify_by_hash_id_updates_requirement(env):
    """#id 目标仍兼容（列表不再展示 id，但内部保留这条通路）。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "#1 加练双指针"))
    assert "已更新课程「算法特训」" in text and "要求=加练双指针" in text
    assert "#1" not in text
    assert db.get_group_course(GID, 1)["requirement"] == "加练双指针"


def test_modify_title_with_spaces_falls_back_to_longest_match(env):
    """带空格的课程名：首 token 匹配不到时按 2 个 token 精确命中。"""
    _text(commands.cmd_training_add(
        GID, TEACHER, "标题=算法 特训 时间=周四 19-21点 地点=二教B201"))
    text = _text(commands.cmd_training_modify(GID, TEACHER, "算法 特训 周五 19点半-21点"))
    assert "已更新课程" in text
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周五", "19:30", "21:00")
    assert course["title"] == "算法 特训"


# ---------------------------------------------------------------- 键值对与混合模式

def test_modify_named_time_field(env):
    """键值对 时间= 单独使用（老写法）仍然有效。"""
    _add("算法特训")
    _text(commands.cmd_training_modify(GID, TEACHER, "1 时间=周三 20:00-22:00"))
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周三", "20:00", "22:00")


def test_modify_mixed_time_and_location_fields(env):
    """`时间=` 与 `地点=` 混写：时间和地点同时更新。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(
        GID, TEACHER, "1 时间=周四 19:00-21:00 地点=二教B302"))
    assert "地点=二教B302" in text
    course = db.get_group_course(GID, 1)
    assert course["location"] == "二教B302"
    assert (course["start_time"], course["end_time"]) == ("19:00", "21:00")


def test_modify_named_time_wins_over_positional_span(env):
    """键值对优先：同时给位置时间和 时间= 时按 时间= 更新，并提示忽略位置时间。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(
        GID, TEACHER, "1 周四 19-21点 时间=周五 20:00-22:00"))
    assert "已忽略位置参数里的时间" in text
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周五", "20:00", "22:00")


# ---------------------------------------------------------------- 失败与歧义

def test_modify_unknown_target_reports_not_found(env):
    _add("算法特训")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "不存在的课 周四 19-21点"))
    assert "找不到课程：不存在的课" in text
    assert db.get_group_course(GID, 1)["weekday"] == "周四"


def test_modify_ambiguous_title_reports_conflict(env):
    """同名课程命中多门时提示改用序号，绝不瞎猜。"""
    _add("训练", "周四 19-21点 二教B201")
    _add("训练", "周五 19-21点 二教B202")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "训练 地点=B303"))
    assert "匹配到多门课程" in text
    assert db.get_group_course(GID, 1)["location"] == "二教B201"


def test_modify_update_false_reports_title_without_unbound_error(env, monkeypatch):
    """落库失败（update_group_course 返回 False）时用课程标题回报，不再引用未定义的 target。"""
    _add("算法特训")
    monkeypatch.setattr(db, "update_group_course", lambda *a, **kw: False)
    text = _text(commands.cmd_training_modify(GID, TEACHER, "1 地点=B303"))
    assert "找不到课程：算法特训" in text
    assert "target" not in text


def test_modify_update_false_by_hash_id_reports_title(env, monkeypatch):
    """#id 目标同样走假值分支：回执仍是标题，不抛异常也不暴露 id。"""
    _add("图论进阶")
    monkeypatch.setattr(db, "update_group_course", lambda *a, **kw: False)
    text = _text(commands.cmd_training_modify(GID, TEACHER, "#1 地点=B303"))
    assert "找不到课程：图论进阶" in text
    assert "#1" not in text
    assert db.get_group_course(GID, 1)["location"] == "二教B201"


def test_modify_missing_target_shows_usage(env):
    """只给字段不给目标时回用法，不误改任何课程。"""
    _add("算法特训")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "地点=B303"))
    assert "用法" in text
    assert db.get_group_course(GID, 1)["location"] == "二教B201"


# ---------------------------------------------------------------- 列表序号口径

def test_list_shows_one_based_index_without_hash_id(env):
    """列表只留 1-based 序号，不再出现 `#id`；序号仍可用于 /课程修改。"""
    _add("算法特训", "周四 19-21点 二教B201")
    _add("图论进阶", "周五 20-22点 二教B202")
    text = _text(commands.cmd_training_list(GID, OUTSIDER, ""))
    assert "#" not in text
    sessions = db.list_training_sessions(GID)
    rows = [line for line in text.splitlines() if line[:1].isdigit()]
    assert len(rows) == len(sessions) == 2
    for index, (row, session) in enumerate(zip(rows, sessions), 1):
        assert row.startswith(f"{index}. {session['title']}")
    # 列表里看到的序号直接可用于修改
    assert "已更新课程" in _text(commands.cmd_training_modify(GID, TEACHER, "2 地点=二教B303"))
    assert db.get_group_course(GID, sessions[1]["id"])["location"] == "二教B303"


# ---------------------------------------------------------------- 工作区标牌

def _bind_group(monkeypatch, gid: str) -> None:
    monkeypatch.setattr(admin_session, "get_active_group", lambda sender: gid)


def test_header_prefers_real_group_number(env, monkeypatch):
    """认领过真实群号时，括号里展示群号而不是 openid。"""
    _bind_group(monkeypatch, GID)
    monkeypatch.setattr(db, "get_entity_alias",
                        lambda kind, openid: {"display_name": "集训队二群", "numeric_id": "123456789"})
    header = commands._private_header("root")
    assert header == "📌【当前工作区：集训队二群 (123456789)】"


def test_header_falls_back_to_name_without_number(env, monkeypatch):
    """只有备注名、没认领群号时只显示名称，不把 openid 塞进括号。"""
    _bind_group(monkeypatch, GID)
    monkeypatch.setattr(db, "get_entity_alias",
                        lambda kind, openid: {"display_name": "集训队二群", "numeric_id": ""})
    header = commands._private_header("root")
    assert header == "📌【当前工作区：集训队二群】"
    assert GID not in header


def test_header_shows_group_number_when_name_missing(env, monkeypatch):
    """没有备注名但有真实群号时，显示「群 <群号>」。"""
    _bind_group(monkeypatch, GID)
    monkeypatch.setattr(db, "get_entity_alias",
                        lambda kind, openid: {"display_name": "", "numeric_id": "123456789"})
    monkeypatch.setattr(db, "get_account_label", lambda kind, account_id: "")
    header = commands._private_header("root")
    assert header == "📌【当前工作区：群 123456789】"


def test_header_falls_back_to_group_id_when_nothing_known(env, monkeypatch):
    """名字和群号都没有时退回原「群 <id/openid>」。"""
    _bind_group(monkeypatch, GID)
    monkeypatch.setattr(db, "get_entity_alias", lambda kind, openid: None)
    monkeypatch.setattr(db, "get_account_label", lambda kind, account_id: "")
    header = commands._private_header("root")
    assert header == f"📌【当前工作区：群 {GID}】"
