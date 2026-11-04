"""修正命令参数回归：完整字段值、错误零写入及版本/审计一致性。"""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import commands
import config
import database


@pytest.fixture
def competition(monkeypatch):
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: connection)
    monkeypatch.setattr(database, "now_str", lambda: "2026-11-01 08:00:00")
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    database.init_db()
    cid = database.create_competition({
        "name": "原比赛",
        "official_reg_deadline": "2026-11-10 23:59",
        "official_time": "2026-12-05 09:00",
        "official_rules": "原规则",
        "summary": "原摘要",
    }, status="confirmed", operator="teacher")
    database.add_pending_update(cid, {"summary": "待审摘要"}, created_by="crawler:auto")
    state = SimpleNamespace(
        cid=cid,
        original=database.get_competition(cid),
        versions=database.list_competition_versions(cid),
        pending=database.get_pending_update(cid),
        audit=database.list_audit(),
    )
    yield state
    connection.close()


def _unchanged(state):
    assert database.get_competition(state.cid) == state.original
    assert database.list_competition_versions(state.cid) == state.versions
    assert database.get_pending_update(state.cid) == state.pending
    assert database.list_audit() == state.audit


@pytest.mark.parametrize("fields,expected", [
    (
        "official_reg_deadline=2026-12-01 23:59 time=2026-12-10 09:30 name=ICPC 2026 区域赛",
        {"official_reg_deadline": "2026-12-01 23:59", "official_time": "2026-12-10 09:30", "name": "ICPC 2026 区域赛"},
    ),
    (
        "name=校赛,edition=2026，summary=更新完成",
        {"name": "校赛", "edition": "2026", "summary": "更新完成"},
    ),
    (
        'name="ICPC 2026 区域赛" summary=\'学校统一报名\'',
        {"name": "ICPC 2026 区域赛", "summary": "学校统一报名"},
    ),
    (
        "name=“ICPC 2026 区域赛” summary=‘学校统一报名’",
        {"name": "ICPC 2026 区域赛", "summary": "学校统一报名"},
    ),
    (
        "site=https://contest.example.com/news?a=1&b=2 rules=Alice's team，先报名,再提交",
        {"official_site": "https://contest.example.com/news?a=1&b=2", "official_rules": "Alice's team，先报名,再提交"},
    ),
    (
        'rules="请填 name=姓名，time=时间" site="https://contest.example.com/x?a=1,b=2&k=v"',
        {"official_rules": "请填 name=姓名，time=时间", "official_site": "https://contest.example.com/x?a=1,b=2&k=v"},
    ),
    (
        r'summary="path C:\team\notes; say \"hi\"; literal \\done"',
        {"summary": r'path C:\team\notes; say "hi"; literal \done'},
    ),
    (
        'reg_deadline= time= summary=""',
        {"official_reg_deadline": "", "official_time": "", "summary": ""},
    ),
])
async def test_complete_values_are_committed_with_version_and_audit(competition, fields, expected):
    state = competition
    replies = await commands.handle_command(f"/修正 {state.cid} {fields}", "g1", "admin")
    stored = database.get_competition(state.cid)
    assert {field: stored[field] for field in expected} == expected
    assert stored["status"] == "confirmed"
    assert {reply["group_id"] for reply in replies} == {"g1", "g2"}
    assert "已修正" in replies[0]["text"]
    versions = database.list_competition_versions(state.cid)
    assert len(versions) == len(state.versions) + 1
    assert versions[0]["kind"] == "amend"
    assert versions[0]["created_by"] == "admin"
    saved_fields = json.loads(versions[0]["fields_json"])
    assert {field: saved_fields[field] for field in expected} == expected
    audits = database.list_audit()
    assert len(audits) == len(state.audit) + 1
    assert audits[0]["operator"] == "admin"
    assert audits[0]["action"] == "amend"
    assert json.loads(audits[0]["after_json"]) == expected
    assert database.get_pending_update(state.cid) is None


@pytest.mark.parametrize("fields", [
    "name=已修正 unknown=不支持",
    "unknown=不支持 name=已修正",
    'name=已修正 summary="未闭合',
    "name=已修正 summary=‘未闭合",
    'name="已修正"额外文本',
    'name="已修正" 额外文本',
    'name="已修正"summary=摘要',
    "name=已修正 time=2026-12-01 09:00 official_time=2026-12-02 09:00",
    "name=已修正 summary=第一条 summary=第二条",
    "name=已修正 official_reg_deadline=2026-02-30 23:59",
    "name=已修正 official_time=2026-12-01 25:61",
    'summary=已修正 name=""',
    "无字段格式 name=已修正",
])
async def test_bad_later_fields_never_partially_amend(competition, fields):
    state = competition
    replies = await commands.handle_command(f"/修正 {state.cid} {fields}", "g1", "admin")
    _unchanged(state)
    assert len(replies) == 1
    assert replies[0]["group_id"] == "g1"
    assert "已修正" not in replies[0]["text"].split("：", 1)[0]


async def test_permission_is_checked_before_parser_or_database(competition, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("未授权请求不应解析参数或读取比赛")

    monkeypatch.setattr(commands, "_parse_amend_fields", forbidden)
    monkeypatch.setattr(database, "get_competition", forbidden)
    replies = await commands.handle_command('/修正 1 name="坏引号', "g1", "member")
    assert replies == [{"group_id": "g1", "text": "仅超管可修正比赛信息"}]


async def test_refetch_still_routes_before_field_parser(competition, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("重新提取不应走字段解析")

    monkeypatch.setattr(commands, "_parse_amend_fields", forbidden)
    expected = [{"group_id": "g1", "text": "模拟重新提取结果"}]
    callback = AsyncMock(return_value=expected)
    monkeypatch.setattr(commands, "_amend_refetch", callback)
    rest = "重新提取 https://contest.example.com/x?a=1,b=2"
    replies = await commands.handle_command(f"/修正 {competition.cid} {rest}", "g1", "admin")
    assert replies == expected
    callback.assert_awaited_once_with("g1", competition.cid, competition.original, rest)
    _unchanged(competition)
