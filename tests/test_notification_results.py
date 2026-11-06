"""Protocol-neutral notification and delivery error tests."""
from types import SimpleNamespace

import pytest

from cdutetc import config
from cdutetc.errors import (
    AppErrorCode, DeliveryCode, DeliveryMode, classify_exception,
    result_from_exception, result_from_onebot, safe_error_hint,
)
from cdutetc.notifications import send_group, send_private


class StatusError(Exception):
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (StatusError(401), DeliveryCode.AUTHENTICATION),
        (StatusError(403), DeliveryCode.FORBIDDEN),
        (StatusError(404), DeliveryCode.NOT_FOUND),
        (StatusError(405), DeliveryCode.UNSUPPORTED),
        (StatusError(429), DeliveryCode.RATE_LIMITED),
        (StatusError(500), DeliveryCode.PLATFORM_ERROR),
        (TimeoutError(), DeliveryCode.TIMEOUT_UNKNOWN),
        (RuntimeError("sensitive details"), DeliveryCode.UNKNOWN),
    ],
)
def test_documented_sdk_http_error_mapping(error, expected):
    assert classify_exception(error) == expected


def test_error_serialization_does_not_leak_exception_text():
    result = result_from_exception(
        RuntimeError("secret user id and request body"), protocol="qqbot", mode=DeliveryMode.ACTIVE
    )
    assert result.code == DeliveryCode.UNKNOWN
    assert "secret" not in result.user_hint
    assert result.user_hint.startswith("[unknown]")
    timeout = result_from_exception(
        TimeoutError(), protocol="qqbot", mode=DeliveryMode.ACTIVE
    )
    assert "本次发送未确认" in timeout.user_hint and "重试可能造成重复" in timeout.user_hint
    assert safe_error_hint(AppErrorCode.VALIDATION).startswith("[validation]")


def test_onebot_receipt_requires_status_retcode_and_message_id():
    assert result_from_onebot(
        {"status": "ok", "retcode": 0, "data": {"message_id": 3}},
        action="send_group_msg",
    )
    assert result_from_onebot(None, action="send_group_msg").code == DeliveryCode.TIMEOUT_UNKNOWN
    assert result_from_onebot(
        {"status": "ok", "retcode": 0, "data": {}}, action="send_group_msg"
    ).code == DeliveryCode.UNKNOWN


async def test_notification_entry_routes_result_and_explicit_reply_seq(monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    class Client:
        async def send_group_msg_result(self, target, text, **kwargs):
            return SimpleNamespace(target=target, text=text, kwargs=kwargs)
        async def send_private_msg_result(self, target, text, **kwargs):
            return SimpleNamespace(target=target, text=text, kwargs=kwargs)
    client = Client()
    group = await send_group(client, "g", "reminder")
    assert group.kwargs == {"reply_to": None}
    passive = await send_group(client, "g", "reply", reply_to="msg-1", reply_seq=2)
    assert passive.kwargs == {"reply_to": "msg-1", "reply_seq": 2}
    private = await send_private(client, "u", "notice")
    assert private.kwargs == {"reply_to": None}


async def test_missing_detailed_api_returns_structured_unsupported(monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "onebot")
    result = await send_group(object(), "g", "text")
    assert not result
    assert result.code == DeliveryCode.UNSUPPORTED
    assert result.protocol == "onebot"
