"""群汇总不含个人数据；管理员本机导出明细的离线回归。"""

import csv
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from botpy.message import GroupMessage

import commands
import config
import database
import main
from scripts.export_collection import export_collection


PERSONAL = ("private-member-1", "private-member-2", "张三", "李四", "2026123456", "姓名", "学号")


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    db_path = tmp_path / "collections.db"
    conn = sqlite3.connect(db_path)
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn, db_path
    conn.close()


@pytest.fixture
def collection(file_db):
    cid = database.create_collection(
        "g1", "集训登记", purpose="参加意向", capacity=3, fields=["姓名", "学号"], operator="teacher",
    )
    assert database.register_collection(
        cid, "g1", "private-member-1", {"姓名": "张三", "学号": "2026123456"},
    )[0]
    assert database.register_collection(
        cid, "g1", "private-member-2", {"姓名": "李四", "学号": "2026987654"},
    )[0]
    assert database.cancel_collection_registration(cid, "g1", "private-member-2")[0]
    return cid


@pytest.fixture
async def bot(file_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    client = main.ACMBot()
    api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "sent-id"}),
        post_c2c_message=AsyncMock(),
    )
    monkeypatch.setattr(client, "api", api)
    return client, api


def _message(api, cid, member, group="g1"):
    return GroupMessage(api=api, event_id="incoming-event", data={
        "id": "incoming-id", "content": f"/征集汇总 {cid}", "group_openid": group,
        "author": {"member_openid": member},
    })


@pytest.mark.parametrize("member", ["teacher", "admin", "student"])
async def test_actual_summary_reply_only_sends_aggregates_to_source_group(bot, collection, monkeypatch, member):
    client, api = bot

    def forbid_detail(*args, **kwargs):
        pytest.fail("群汇总不应读取个人明细")

    monkeypatch.setattr(database, "list_collection_responses", forbid_detail)
    await client.on_group_at_message_create(_message(api, collection, member))

    api.post_group_message.assert_awaited_once()
    sent = api.post_group_message.await_args.kwargs
    assert sent["group_openid"] == "g1" and sent["msg_id"] == "incoming-id"
    assert sent["msg_seq"] == 1 and sent["msg_type"] == 0
    assert "集训登记" in sent["content"] and "参加意向" in sent["content"]
    assert "已报名：1 人" in sent["content"]
    assert "已取消：1 人" in sent["content"]
    assert "剩余名额：2" in sent["content"]
    assert all(value not in sent["content"] for value in PERSONAL)
    assert "明细" not in sent["content"]
    api.post_c2c_message.assert_not_awaited()


@pytest.mark.parametrize("member", ["teacher", "admin", "student"])
async def test_actual_summary_rejects_cross_group_queries(bot, collection, member):
    client, api = bot
    await client.on_group_at_message_create(_message(api, collection, member, group="g2"))
    api.post_group_message.assert_awaited_once()
    sent = api.post_group_message.await_args.kwargs
    assert sent["group_openid"] == "g2" and sent["msg_id"] == "incoming-id"
    assert sent["content"] == "征集不存在或不属于本群"
    api.post_c2c_message.assert_not_awaited()


def test_empty_unlimited_summary_has_no_detail(file_db):
    cid = database.create_collection("g1", "报名")
    text = commands.cmd_collection_summary("g1", "student", str(cid))[0]["text"]
    assert "已报名：0 人" in text and "剩余名额：不限" in text
    assert "明细" not in text


def _read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.reader(file))


def test_export_only_current_matching_group_rows_without_changing_database(file_db, collection, tmp_path):
    conn, db_path = file_db
    other_cid = database.create_collection("g2", "其他群报名", fields=["姓名"])
    assert database.register_collection(other_cid, "g2", "other-group-member", {"姓名": "其他人"})[0]
    with conn:
        conn.execute(
            "INSERT INTO collection_responses (collection_id, group_id, member_id, data_json) VALUES (?, ?, ?, ?)",
            (collection, "g2", "inconsistent-group-member", '{"姓名": "不属于本群"}'),
        )
    before = db_path.read_bytes()
    before_rows = list(conn.iterdump())
    output = tmp_path / "details.csv"

    assert export_collection(db_path, "g1", collection, output) == 1
    assert _read_csv(output) == [
        ["member_id", "data.姓名", "data.学号"],
        ["private-member-1", "张三", "2026123456"],
    ]
    assert db_path.read_bytes() == before
    assert list(conn.iterdump()) == before_rows


@pytest.mark.parametrize("group_id", ["g2", "missing-group", "", "   "])
def test_export_wrong_group_never_creates_output(file_db, collection, tmp_path, group_id):
    output = tmp_path / "details.csv"
    with pytest.raises(ValueError):
        export_collection(file_db[1], group_id, collection, output)
    assert not output.exists()


def test_export_never_overwrites_existing_output(file_db, collection, tmp_path):
    output = tmp_path / "existing.csv"
    output.write_bytes(b"existing-file-must-survive")
    with pytest.raises(FileExistsError):
        export_collection(file_db[1], "g1", collection, output)
    assert output.read_bytes() == b"existing-file-must-survive"


def test_export_missing_database_is_not_created(tmp_path):
    db_path = tmp_path / "missing.db"
    output = tmp_path / "details.csv"
    with pytest.raises(sqlite3.OperationalError):
        export_collection(db_path, "g1", 1, output)
    assert not db_path.exists() and not output.exists()


@pytest.mark.parametrize("raw", ["broken-json", "[]", "null", '{"姓名": 123}', '{"姓名": null}'])
def test_export_invalid_response_fails_before_creating_output(file_db, collection, tmp_path, raw):
    conn, db_path = file_db
    with conn:
        conn.execute("UPDATE collection_responses SET data_json = ? WHERE member_id = ?", (raw, "private-member-1"))
    output = tmp_path / "details.csv"
    with pytest.raises(ValueError, match="登记内容格式错误"):
        export_collection(db_path, "g1", collection, output)
    assert not output.exists()


@pytest.mark.parametrize("raw", ["broken-json", "{}", "null", '["姓名", 123]'])
def test_export_invalid_declared_fields_fails_before_creating_output(file_db, collection, tmp_path, raw):
    conn, db_path = file_db
    with conn:
        conn.execute("UPDATE collections SET fields_json = ? WHERE id = ?", (raw, collection))
    output = tmp_path / "details.csv"
    with pytest.raises(ValueError, match="登记内容格式错误"):
        export_collection(db_path, "g1", collection, output)
    assert not output.exists()


def test_export_quotes_csv_text_and_neutralizes_formula_prefixes(file_db, tmp_path):
    _, db_path = file_db
    cid = database.create_collection("g1", "自由登记", fields=["备注", "姓名", "未填写"])
    # 保存旧版登记允许的额外字段；导出不能静默丢失已有明细。
    data = {"备注": '  =SUM(1,2)', "姓名": '含,逗号"与\n换行', "补充": "@example"}
    with file_db[0] as conn:
        conn.execute(
            "INSERT INTO collection_responses (collection_id, group_id, member_id, data_json) VALUES (?, ?, ?, ?)",
            (cid, "g1", "+formula-member", json.dumps(data, ensure_ascii=False)),
        )
    output = tmp_path / "details.csv"
    assert export_collection(db_path, "g1", cid, output) == 1
    assert _read_csv(output) == [
        ["member_id", "data.备注", "data.姓名", "data.未填写", "data.补充"],
        ["'+formula-member", "'  =SUM(1,2)", '含,逗号"与\n换行', "", "'@example"],
    ]
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")


def test_export_empty_collection_writes_declared_header(file_db, tmp_path):
    cid = database.create_collection("g1", "无人报名", fields=["姓名", "学号"])
    output = tmp_path / "details.csv"
    assert export_collection(file_db[1], "g1", cid, output) == 0
    assert _read_csv(output) == [["member_id", "data.姓名", "data.学号"]]


def test_real_cli_uses_configured_database_without_printing_personal_details(file_db, collection, tmp_path):
    output = tmp_path / "details.csv"
    env = dict(os.environ, ACM_DB_PATH=str(file_db[1]), PYTHONIOENCODING="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "export_collection.py"
    args = [sys.executable, str(script), "--group-id", "g1", "--collection-id", str(collection), "--output", str(output)]
    result = subprocess.run(args, env=env, cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0 and "已导出 1 条报名" in result.stdout
    assert result.stderr == ""
    assert all(value not in result.stdout for value in PERSONAL)
    assert _read_csv(output)[1] == ["private-member-1", "张三", "2026123456"]

    failed = subprocess.run(args, env=env, cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=15)
    assert failed.returncode == 1 and "导出失败" in failed.stderr
    assert failed.stdout == ""
    assert all(value not in failed.stderr for value in PERSONAL)
    assert _read_csv(output)[1] == ["private-member-1", "张三", "2026123456"]
