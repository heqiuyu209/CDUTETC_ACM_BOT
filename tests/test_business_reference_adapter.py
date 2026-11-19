"""官方群消息只保留结构化引用 ID，晨报成功后绑定本群业务上下文。"""

from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, daily_digest, main
from cdutetc import database as db
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message
from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult

NOW = datetime(2026, 11, 19, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(group="g1", reference=None):
    data = {"id": "teacher-input", "group_openid": group,
            "author": {"member_openid": "teacher"}, "content": "地点，93501"}
    if reference is not None:
        data["message_reference"] = reference
    return data


@pytest.mark.parametrize("wrap", [wrap_group_at_message, wrap_group_message])
def test_dict_event_reference_is_platform_id_only(wrap):
    message = wrap(_payload(reference={"message_id": "digest-output", "content": "偽造引用正文"}))
    assert message.reference_id == "digest-output"
    assert message.content == "地点，93501"
    assert "message_reference" not in message.raw
    assert not hasattr(message, "reference_content")


def test_sdk_group_event_preserves_reference_without_quoted_body():
    from botpy.message import GroupMessage

    sdk = GroupMessage(None, "evt", _payload(reference={"message_id": "digest-output", "content": "伪造引用正文"}))
    assert sdk.message_reference.message_id == "digest-output"
    assert not hasattr(sdk.message_reference, "content")
    assert wrap_group_at_message(sdk).reference_id == "digest-output"
    assert wrap_group_message(sdk).reference_id == "digest-output"


@pytest.mark.parametrize("reference", [None, {}, {"message_id": None}, {"message_id": 123},
                                        {"message_id": True}, {"message_id": ["digest"]},
                                        {"message_id": ""}, {"message_id": "x" * 257},
                                        {"id": "digest-output"}, {"content": "[reply:digest-output]"}])
def test_missing_invalid_or_invented_reference_is_empty(reference):
    assert wrap_group_message(_payload(reference=reference)).reference_id == ""


def test_reference_id_accepts_maximum_length_without_truncation():
    reference = "x" * 256
    assert wrap_group_message(_payload(reference={"message_id": reference})).reference_id == reference


def test_content_reply_markers_do_not_create_reference():
    payload = _payload()
    payload["content"] = "[CQ:reply,id=digest-output] 引用：早报 地点，93501"
    assert wrap_group_message(payload).reference_id == ""


def test_same_reference_id_stays_associated_with_separate_groups():
    first = wrap_group_message(_payload("g1", {"message_id": "digest-output"}))
    second = wrap_group_message(_payload("g2", {"message_id": "digest-output"}))
    assert first.reference_id == second.reference_id
    assert first.group_id == "g1" and second.group_id == "g2"


@pytest.fixture
def jobs_env(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "references.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    db.init_db()
    return main.ACMBot(official_client)


def _detail(group, obj_id):
    return {"group_id": group, "text": f"本群早报 {group}", "contest_enabled": False,
            "today_contest_count": 0, "sessions": [{"id": obj_id, "title": group,
            "start_text": "19:00", "end_text": "21:00", "location": ""}],
            "competitions": [], "reminders": []}


async def test_successful_digest_binds_returned_message_id_with_own_detail(jobs_env, monkeypatch):
    from cdutetc import business_followup

    details = {"g1": _detail("g1", 1), "g2": _detail("g2", 2)}
    monkeypatch.setattr(daily_digest, "collect_daily_digest", lambda group, now=None: details[group])
    remember = Mock()
    monkeypatch.setattr(business_followup, "remember_digest", remember)

    async def send(group, text):
        assert text == details[group]["text"]
        return DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE, "same-platform-id")

    monkeypatch.setattr(jobs_env, "_send_group_result", send)
    assert await jobs_env._daily_digest_job(now=NOW) == 2
    assert remember.call_count == 2
    remember.assert_any_call("g1", "same-platform-id", details["g1"], now=NOW)
    remember.assert_any_call("g2", "same-platform-id", details["g2"], now=NOW)


@pytest.mark.parametrize("result", [DeliveryResult(False, DeliveryCode.NETWORK_ERROR, "qqbot"),
                                    DeliveryResult(False, DeliveryCode.FORBIDDEN, "qqbot", message_id="rejected-id"),
                                    DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE)])
async def test_failed_or_no_id_digest_is_not_bound(jobs_env, monkeypatch, result):
    from cdutetc import business_followup

    monkeypatch.setattr(daily_digest, "collect_daily_digest", lambda group, now=None: _detail(group, 1))
    remember = Mock()
    monkeypatch.setattr(business_followup, "remember_digest", remember)

    async def send(group, text):
        return result

    monkeypatch.setattr(jobs_env, "_send_group_result", send)
    assert await jobs_env._daily_digest_job(now=NOW) == (2 if result.ok else 0)
    remember.assert_not_called()


async def test_binding_failure_does_not_reclassify_sent_digest_or_stop_next_group(jobs_env, monkeypatch, caplog):
    from cdutetc import business_followup

    monkeypatch.setattr(daily_digest, "collect_daily_digest", lambda group, now=None: _detail(group, 1))
    remember = Mock(side_effect=RuntimeError("offline binding failure"))
    monkeypatch.setattr(business_followup, "remember_digest", remember)
    sent = []

    async def send(group, text):
        sent.append(group)
        return DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE, group + "-digest")

    monkeypatch.setattr(jobs_env, "_send_group_result", send)
    assert await jobs_env._daily_digest_job(now=NOW) == 2
    assert sent == ["g1", "g2"] and remember.call_count == 2
    assert "业务引用绑定失败" in caplog.text
