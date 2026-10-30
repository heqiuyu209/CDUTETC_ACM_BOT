"""qq-botpy 1.2.1 断线重试回执兼容；不修改安装目录。"""

from contextvars import ContextVar

from botpy.http import BotHttp


_retry_receipt = ContextVar("qq_http_retry_receipt", default=None)


class ReceiptBotHttp(BotHttp):
    """保留同一请求递归重试的返回值，并发请求各自隔离。

    SDK 1.2.1 的 ConnectionResetError 分支递归调用缺少 return。
    这里仍执行 SDK 原始请求、鉴权和重试，仅补回内部实际返回的回执。
    超时或重试耗尽没有回执时仍返回 None。
    """

    async def request(self, route, retry_time=0, **kwargs):
        token = _retry_receipt.set(None) if retry_time == 0 else None
        try:
            result = await super().request(route, retry_time, **kwargs)
            if result is not None:
                _retry_receipt.set(result)
            return result if result is not None else _retry_receipt.get()
        finally:
            if token is not None:
                _retry_receipt.reset(token)
