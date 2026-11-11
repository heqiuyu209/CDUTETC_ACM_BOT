"""Protocol-neutral delivery result and conservative error classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any


class AppErrorCode(str, Enum):
    VALIDATION = "validation"
    PERMISSION = "permission"
    INTERNAL = "internal"
    FETCH = "fetch"
    LLM = "llm"


_APP_ERROR_HINTS = {
    AppErrorCode.VALIDATION: "[validation] 提交的信息格式不正确，请核对内容后重试。",
    AppErrorCode.PERMISSION: "[permission] 你没有执行此操作的权限。",
    AppErrorCode.INTERNAL: "[internal] 操作暂时无法完成，请稍后重试；如持续失败请联系管理员。",
    AppErrorCode.FETCH: "[fetch] 目标网页暂时无法读取，请检查链接或稍后重试。",
    AppErrorCode.LLM: "[llm] 模型服务暂时不可用，请稍后重试。",
}


def safe_error_hint(code: AppErrorCode) -> str:
    """Return a safe, non-sensitive user-facing application error hint."""
    return _APP_ERROR_HINTS.get(code, _APP_ERROR_HINTS[AppErrorCode.INTERNAL])


class DeliveryCode(str, Enum):
    SENT = "sent"
    DISABLED = "disabled"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    NOT_CONNECTED = "not_connected"
    INVALID_TARGET = "invalid_target"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication"
    FORBIDDEN = "forbidden"
    ACTIVE_FORBIDDEN = "active_forbidden"
    QUOTA_EXHAUSTED = "quota_exhausted"
    NETWORK_ERROR = "network_error"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PLATFORM_ERROR = "platform_error"
    TIMEOUT_UNKNOWN = "timeout_unknown"
    UNSUPPORTED = "unsupported"
    EMPTY_MESSAGE = "empty_message"
    UNKNOWN = "unknown"


class DeliveryMode(str, Enum):
    ACTIVE = "active"
    PASSIVE = "passive"


# 官方主动消息配额/频次耗尽错误码；命中后必须停止重试并熔断该群。
_QUOTA_EXHAUSTED_CODES = frozenset({11253, 11263})


_SAFE_HINTS = {
    DeliveryCode.SENT: "[sent] 平台已确认接受消息。",
    DeliveryCode.DISABLED: "[disabled] 主动通知已关闭。",
    DeliveryCode.TARGET_NOT_ALLOWED: "[target_not_allowed] 目标群未授权接收通知。",
    DeliveryCode.NOT_CONNECTED: "[not_connected] 机器人接入未连接，请检查机器人进程和平台连接。",
    DeliveryCode.INVALID_TARGET: "[invalid_target] 通知目标无效或机器人当前不可访问该会话，请核对群/用户标识。",
    DeliveryCode.INVALID_REQUEST: "[invalid_request] 平台拒绝了消息请求，请检查消息格式和目标参数。",
    DeliveryCode.AUTHENTICATION: "[authentication] 平台鉴权失败，请检查机器人凭证和授权状态。",
    DeliveryCode.FORBIDDEN: "[forbidden] 平台拒绝发送，请检查目标会话权限和机器人主动消息设置。",
    DeliveryCode.ACTIVE_FORBIDDEN: "[active_forbidden] QQ平台拒绝主动消息（40034105）。请检查目标群/会话的“允许主动发送/主动通知”和机器人权限，修复后用 /通知测试 验证。",
    DeliveryCode.QUOTA_EXHAUSTED: "[quota_exhausted] QQ平台主动消息配额已耗尽（11253/11263），今日已停止重试并熔断该群；明日配额恢复后可用 /通知测试 验证。",
    DeliveryCode.NETWORK_ERROR: "[network_error] 发信超时(10s)或网络异常，消息结果未确认；请检查网络与机器人连接后核对目标会话。",
    DeliveryCode.NOT_FOUND: "[not_found] 平台未找到目标会话，请确认机器人仍在目标群或会话有效。",
    DeliveryCode.RATE_LIMITED: "[rate_limited] 平台频率或配额限制了发送，请查看平台配额后再处理。",
    DeliveryCode.PLATFORM_ERROR: "[platform_error] 平台服务暂时无法确认发送结果，请检查平台状态。",
    DeliveryCode.TIMEOUT_UNKNOWN: "[timeout_unknown] 本次发送未确认，重试可能造成重复消息；请核对目标会话与投递记录。",
    DeliveryCode.UNSUPPORTED: "[unsupported] 当前协议客户端不支持详细投递接口。",
    DeliveryCode.EMPTY_MESSAGE: "[empty_message] 消息内容为空，未发送。",
    DeliveryCode.UNKNOWN: "[unknown] 发送结果无法识别，请检查协议日志和平台回执。",
}


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Normalized result; bool(result) stays compatible with legacy success checks."""

    ok: bool
    code: DeliveryCode
    protocol: str
    mode: DeliveryMode = DeliveryMode.ACTIVE
    detail: str = ""
    message_id: str = ""
    platform_code: int | None = None

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, str | bool | int | None]:
        """Safe structured logging payload; excludes raw exception text."""
        return {
            "ok": self.ok,
            "code": self.code.value,
            "protocol": self.protocol,
            "mode": self.mode.value,
            "detail": self.detail,
            "message_id": self.message_id,
            "platform_code": self.platform_code,
        }

    @property
    def user_hint(self) -> str:
        return _SAFE_HINTS[self.code]

    @classmethod
    def sent(cls, protocol: str, mode: DeliveryMode, message_id: str = "") -> "DeliveryResult":
        return cls(True, DeliveryCode.SENT, protocol, mode, message_id=message_id)


def _platform_error_code(error: BaseException) -> int | None:
    """优先读取业务码；botpy老版本丢掉响应码时只识别已知的精确消息。"""
    payload = getattr(error, "msgs", None)
    if isinstance(payload, str) and payload.lstrip().startswith("{") and len(payload) <= 4096:
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            pass
    values = [getattr(error, "err_code", None), getattr(error, "code", None)]
    if isinstance(payload, dict):
        values += [payload.get("err_code"), payload.get("code")]
    for value in values:
        if type(value) is int and value > 0:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
            return int(value)
    # 官方botpy的HTTP 400会抛ServerError，只把data['message']留在msgs里。
    if isinstance(payload, str):
        normalized = re.sub(r"\s+", "", payload).replace("，", ",")
        if normalized in ("主动消息失败,无权限", "主动消息发送失败,无权限"):
            return 40034105
        # 官方主动消息配额/频次错误有时只在 msgs 文本里出现，兜底识别。
        if len(normalized) <= 200:
            for quota_code in _QUOTA_EXHAUSTED_CODES:
                if str(quota_code) in normalized:
                    return quota_code
    return None


def classify_exception(error: BaseException) -> DeliveryCode:
    """Map documented SDK exception classes/statuses; unknown errors stay generic."""
    name = type(error).__name__.lower()
    platform_code = _platform_error_code(error)
    if platform_code == 40034105:
        return DeliveryCode.ACTIVE_FORBIDDEN
    # 11253/11263：官方主动消息配额/频次已耗尽，必须熔断，不能当未知错误反复重试。
    if platform_code in _QUOTA_EXHAUSTED_CODES:
        return DeliveryCode.QUOTA_EXHAUSTED
    if platform_code == 40034100:
        return DeliveryCode.RATE_LIMITED
    status = getattr(error, "status", None)
    if status is None:
        status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 401 or "authenticationfailed" in name:
        return DeliveryCode.AUTHENTICATION
    if status == 403 or "forbidden" in name:
        return DeliveryCode.FORBIDDEN
    if status == 404 or "notfound" in name:
        return DeliveryCode.NOT_FOUND
    if status == 405 or "methodnotallowed" in name:
        return DeliveryCode.UNSUPPORTED
    if status == 429 or "sequencenumber" in name:
        return DeliveryCode.RATE_LIMITED
    if status in (500, 504) or "servererror" in name:
        return DeliveryCode.PLATFORM_ERROR
    if status is not None and 400 <= status < 500:
        return DeliveryCode.INVALID_REQUEST
    if isinstance(error, (TimeoutError,)) or "timeout" in name:
        return DeliveryCode.TIMEOUT_UNKNOWN
    return DeliveryCode.UNKNOWN


def result_from_exception(error: BaseException, *, protocol: str,
                          mode: DeliveryMode) -> DeliveryResult:
    # Do not expose exception text: SDK errors may contain request data or identifiers.
    return DeliveryResult(False, classify_exception(error), protocol, mode,
                          detail=type(error).__name__, platform_code=_platform_error_code(error))


def result_from_onebot(response: Any, *, action: str, connected: bool = True) -> DeliveryResult:
    """Normalize OneBot action response using status/retcode/data contract."""
    mode = DeliveryMode.ACTIVE
    if not connected:
        return DeliveryResult(False, DeliveryCode.NOT_CONNECTED, "onebot", mode)
    if not isinstance(response, dict):
        code = DeliveryCode.TIMEOUT_UNKNOWN if response is None else DeliveryCode.UNKNOWN
        return DeliveryResult(False, code, "onebot", mode,
                              detail="missing_response" if response is None else "invalid_response")
    status = response.get("status")
    retcode = response.get("retcode")
    if status == "ok" and type(retcode) is int and retcode == 0:
        if response.get("message") or response.get("wording"):
            return DeliveryResult(False, DeliveryCode.UNKNOWN, "onebot", mode,
                                  detail="error_message_in_success_response")
        data = response.get("data")
        if isinstance(data, dict) and (data.get("error") or data.get("wording")):
            return DeliveryResult(False, DeliveryCode.UNKNOWN, "onebot", mode,
                                  detail="error_message_in_data")
        mid = data.get("message_id", "") if isinstance(data, dict) else ""
        if action.startswith("send_"):
            invalid_id = isinstance(mid, bool) or not str(mid).strip()
            if isinstance(mid, (int, float)) and mid <= 0:
                invalid_id = True
            if isinstance(mid, str) and mid.strip().isdigit() and int(mid.strip()) <= 0:
                invalid_id = True
            if invalid_id:
                return DeliveryResult(False, DeliveryCode.UNKNOWN, "onebot", mode,
                                      detail="missing_message_id")
        return DeliveryResult.sent("onebot", mode, str(mid or ""))
    return DeliveryResult(False, DeliveryCode.UNKNOWN, "onebot", mode,
                          detail=f"retcode:{retcode if retcode is not None else 'missing'}")
