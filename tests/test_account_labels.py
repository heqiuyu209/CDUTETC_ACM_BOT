"""标识别名测试：官方机器人只给 openid，命名后要能显示成可读名字。"""

import json
import sqlite3

import pytest

from cdutetc import database
from cdutetc import main


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


GROUP = "A0EF5D2DFBA4B3CC76E3C70C47971FDE"
USER = "DCF4F26867BD71753336F962C626F3FD"


def test_set_and_get_label(memory_db):
    assert database.get_account_label("group", GROUP) == ""
    database.set_account_label("group", GROUP, "集训队群", "admin")
    assert database.get_account_label("group", GROUP) == "集训队群"


def test_label_is_upserted(memory_db):
    database.set_account_label("group", GROUP, "旧名字", "admin")
    database.set_account_label("group", GROUP, "集训队群", "admin")
    assert database.get_account_label("group", GROUP) == "集训队群"
    assert len(database.list_account_labels("group")) == 1


def test_group_and_user_labels_are_separate(memory_db):
    database.set_account_label("group", GROUP, "集训队群", "admin")
    database.set_account_label("user", USER, "张老师", "admin")
    assert database.get_account_label("group", GROUP) == "集训队群"
    assert database.get_account_label("user", USER) == "张老师"


def test_describe_account_without_label_is_shortened(memory_db):
    text = database.describe_account("group", GROUP)
    assert "A0EF5D2D" in text and "1FDE" in text
    assert len(text) < len(GROUP) + 4


def test_describe_account_with_label(memory_db):
    database.set_account_label("group", GROUP, "集训队群", "admin")
    text = database.describe_account("group", GROUP)
    assert "集训队群" in text
    assert "A0EF5D2D" in text  # 仍保留缩写便于核对


def test_publish_review_shows_labels(memory_db):
    database.set_account_label("group", GROUP, "集训队群", "admin")
    database.set_account_label("user", USER, "张老师", "admin")
    info = {
        "name": "蓝桥杯", "edition": "", "official_reg_start": "",
        "official_reg_deadline": "", "official_time": "", "tracks": "",
        "device_req": "", "summary": "", "official_site": "",
    }
    pending = {
        "id": 1, "fields_json": json.dumps(info, ensure_ascii=False),
        "group_id": GROUP, "submitted_by": USER, "duplicate_note": "",
    }
    text = main._build_publish_review(pending)
    assert "来源群：集训队群" in text
    assert "提交人：张老师" in text
    # 不应再出现裸 openid
    assert f"来源群：{GROUP}" not in text


def test_remove_label(memory_db):
    database.set_account_label("user", USER, "张老师", "admin")
    assert database.remove_account_label("user", USER, "admin") is True
    assert database.get_account_label("user", USER) == ""


def test_invalid_kind_raises(memory_db):
    with pytest.raises(ValueError):
        database.set_account_label("robot", USER, "x", "admin")
