"""阶段二：三层数据模型（公共比赛/学校安排/群内安排）与跨群隔离、
老师发布/修正管理、课程与 FAQ、未答问题收集、旧库安全迁移（内存 SQLite 离线测试）。"""

import sqlite3

import pytest

import database


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


def _comp(**overrides):
    info = {
        "name": "ICPC 2026 区域赛",
        "edition": "2026",
        "official_site": "https://icpc.example.com",
        "official_reg_deadline": "2026-11-10 23:59",
        "official_time": "2026-12-05 09:00",
        "tracks": "ICPC/EC-Final",
        "official_rules": "官方规则",
        "device_req": "",
        "summary": "",
    }
    info.update(overrides)
    return info


# ---------- 公共比赛：发布/修正与版本 ----------


def test_competition_draft_and_confirm(db):
    cid = database.create_competition(_comp())
    c = database.get_competition(cid)
    assert c["status"] == "draft"
    assert c["verified_at"] == ""
    assert database.confirm_competition(cid, operator="teacher1") is True
    c = database.get_competition(cid)
    assert c["status"] == "confirmed"
    assert c["verified_at"]
    # 已确认不能再重复确认
    assert database.confirm_competition(cid, operator="teacher1") is False


def test_competition_reject_keeps_draft(db):
    cid = database.create_competition(_comp())
    assert database.reject_competition(cid, operator="teacher1", note="信息不全")
    assert database.get_competition(cid)["status"] == "draft"
    versions = database.list_competition_versions(cid)
    assert any(v["change_note"].startswith("拒绝") for v in versions)


def test_competition_invalid_schema_no_write(db):
    with pytest.raises(ValueError):
        database.create_competition({"edition": "2026"})  # 缺必填 name
    assert len(database.list_competitions()) == 0


def test_competition_update_versions(db):
    cid = database.create_competition(_comp())
    database.update_competition(cid, {"official_reg_deadline": "2026-11-20 23:59"},
                                operator="t", note="老师调整")
    assert database.get_competition(cid)["official_reg_deadline"] == "2026-11-20 23:59"
    assert len(database.list_competition_versions(cid)) == 2


def test_set_competition_status_roundtrip(db):
    cid = database.create_competition(_comp())
    database.confirm_competition(cid, operator="t")
    assert database.set_competition_status(cid, "draft", operator="crawler",
                                           note="来源变化") is True
    assert database.get_competition(cid)["status"] == "draft"
    assert database.set_competition_status(cid, "draft", operator="crawler") is False


# ---------- 学校/集训队统一安排 ----------


def test_org_event_shareable_filter(db):
    e1 = database.create_org_event({
        "title": "校队集训", "event_type": "集训",
        "reg_deadline": "2026-11-01 10:00", "contest_time": "2026-11-03 14:00",
        "location": "机房A", "shareable": True,
    })
    e2 = database.create_org_event({"title": "内部通知", "shareable": False})
    assert len(database.list_org_events()) == 2
    assert [x["id"] for x in database.list_org_events(shareable_only=True)] == [e1]
    assert database.confirm_org_event(e1, operator="t") is True
    assert [x["id"] for x in database.list_org_events(status="confirmed")] == [e1]
    assert database.update_org_event(e1, {"location": "机房B"}) is True
    assert [x["location"] for x in database.list_org_events(shareable_only=True)][0] == "机房B"


# ---------- 群内课程安排 ----------


def test_training_sessions_group_isolation(db):
    t1 = database.create_training_session("g1", {
        "title": "算法课", "weekday": "周三", "start_time": "19:00",
        "end_time": "21:00", "location": "机房", "needs_laptop": True,
    })
    database.create_training_session("g2", {"title": "集训", "weekday": "周六"})
    assert len(database.list_training_sessions("g1")) == 1
    assert len(database.list_training_sessions("g2")) == 1
    assert database.cancel_training_session("g2", t1) is False  # 跨群取消被拒
    assert database.cancel_training_session("g1", t1, operator="t") is True
    assert len(database.list_training_sessions("g1")) == 0  # active_only 不含已取消


# ---------- FAQ ----------


def test_faq_group_isolation_and_delete(db):
    f1 = database.add_faq("g1", "比赛怎么报名？", "找老师报名", source="老师")
    database.add_faq("g2", "场地在哪？", "机房")
    assert len(database.list_faq("g1")) == 1
    assert len(database.list_faq("g2")) == 1
    assert len(database.list_faq("g1", keyword="报名")) == 1
    assert len(database.list_faq("g1", keyword="场地")) == 0
    assert database.delete_faq("g2", f1) is False  # 跨群删除被拒
    assert database.delete_faq("g1", f1) is True
    assert len(database.list_faq("g1")) == 0
    with pytest.raises(ValueError):
        database.add_faq("g1", "", "答案")


# ---------- 未答问题收集 ----------


def test_unanswered_questions(db):
    q1 = database.add_unanswered_question("g1", "什么是CDUTETC？", asker="m1")
    q_same = database.add_unanswered_question("g1", "什么是CDUTETC？", asker="m2")
    assert q1 == q_same  # 同题合并，不重复收集
    database.add_unanswered_question("g2", "别的群的问题")
    assert len(database.list_unanswered("g1")) == 1
    assert len(database.list_unanswered("g2")) == 1
    fid = database.add_faq("g1", "什么是CDUTETC？", "CDUTETC ACM 队")
    assert database.mark_question_answered(q1, faq_id=fid) is True
    assert len(database.list_unanswered("g1")) == 0
    with pytest.raises(ValueError):
        database.add_unanswered_question("g1", "")


# ---------- 旧库安全迁移 ----------


def test_old_db_safe_migration(db):
    old1 = database.save_contest("", {"name": "2025 校赛", "source_link": "https://old.example.com"})
    database.save_contest("g1", {"name": "2025 区域赛"})
    unclaimed = database.list_unclaimed_contests()
    assert len(unclaimed) == 1 and unclaimed[0]["id"] == old1

    # 升级：旧记录 → 公共比赛 + 来源，原记录保留并归属群（v2 不再生成群订阅）
    comp_id = database.migrate_contest_to_competition(old1, "g1", operator="t")
    assert comp_id is not None
    comp = database.get_competition(comp_id)
    assert comp["status"] == "draft"
    assert comp["name"] == "2025 校赛"
    assert len(database.list_sources(comp_id)) == 1
    assert all(c["id"] != old1 for c in database.list_unclaimed_contests())
    assert any(c["id"] == old1 and c["group_id"] == "g1" for c in database.list_contests("g1"))

    # claim 流程：显式认领后不可再认领
    old2 = database.save_contest("", {"name": "2024 校赛"})
    assert database.claim_contest(old2, "g2") is True
    assert database.claim_contest(old2, "g3") is False
    assert database.migrate_contest_to_competition(999999, "g1") is None
