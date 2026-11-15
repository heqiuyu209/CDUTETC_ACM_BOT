"""官方机器人收图链路测试：attachments 规范化、source 兜底与 URL 协议补全。

覆盖三种线上真实形态：
  1) botpy SDK 下发的 Attachment 对象（属性访问）；
  2) 原始事件字典里的 attachments（键访问，含非图片附件）；
  3) 地址缺协议头（``//host/...`` 与裸域名），httpx 无法直接请求。
"""

from types import SimpleNamespace

from cdutetc import memes
from cdutetc.clients.qqbot import QQBotMessage, wrap_group_at_message


def _attachment_object(url: str, filename: str = "", content_type: str = "image/png"):
    """模拟 botpy 的 Attachment 模型：只有属性、没有字典键。"""
    return SimpleNamespace(url=url, filename=filename, content_type=content_type)


# ---------- QQBotMessage：attachments 同步进 raw ----------


def test_message_syncs_sdk_attachment_objects_into_raw():
    message = SimpleNamespace(
        content="",
        group_openid="GROUP_OPENID",
        id="MSG1",
        author=SimpleNamespace(member_openid="MEMBER_OPENID"),
        attachments=[
            _attachment_object(
                "https://multimedia.nt.qq.com.cn/download?fileid=abc",
                "abc.png",
                "image/png",
            )
        ],
    )
    wrapped = wrap_group_at_message(message)

    assert wrapped.raw["attachments"] == [
        {
            "url": "https://multimedia.nt.qq.com.cn/download?fileid=abc",
            "filename": "abc.png",
            "content_type": "image/png",
        }
    ]
    # 原始对象仍保留在 source，便于排查
    assert wrapped.source is message


def test_message_syncs_raw_dict_attachments_into_raw():
    data = {
        "content": "",
        "group_openid": "G",
        "id": "M2",
        "attachments": [
            {"url": "//multimedia.nt.qq.com.cn/a.jpg", "filename": "a.jpg",
             "content_type": "image/jpeg"},
            {"url": "", "filename": ""},
        ],
    }
    wrapped = QQBotMessage(data, message_type="group", group_id="G", msg_id="M2")

    # 空附件（既无地址也无文件名）被丢弃
    assert wrapped.raw["attachments"] == [
        {
            "url": "//multimedia.nt.qq.com.cn/a.jpg",
            "filename": "a.jpg",
            "content_type": "image/jpeg",
        }
    ]


def test_message_without_attachments_keeps_raw_empty():
    message = SimpleNamespace(content="你好", group_openid="G", id="M3")
    wrapped = wrap_group_at_message(message)
    assert wrapped.raw == {}


# ---------- extract_image_refs：从 attachments 抠图 ----------


def test_extract_reads_attachments_from_wrapped_message():
    message = SimpleNamespace(
        content="",
        group_openid="G",
        id="M4",
        author=SimpleNamespace(member_openid="U"),
        attachments=[_attachment_object("https://example.com/x.png", "x.png")],
    )
    wrapped = wrap_group_at_message(message)

    refs = memes.extract_image_refs(wrapped)
    assert refs == [{"url": "https://example.com/x.png", "file": "x.png"}]


def test_extract_falls_back_to_source_attachments():
    """raw 里没有 attachments（旧调用路径）时读原始事件对象。"""
    message = SimpleNamespace(
        content="",
        attachments=[
            _attachment_object("multimedia.nt.qq.com.cn/download?fileid=1", "1.jpg",
                               "image/jpeg")
        ],
    )
    wrapped = QQBotMessage(message, message_type="group", group_id="G", msg_id="M5")
    wrapped.raw.pop("attachments", None)

    refs = memes.extract_image_refs(wrapped)
    assert refs == [
        {"url": "https://multimedia.nt.qq.com.cn/download?fileid=1", "file": "1.jpg"}
    ]


def test_extract_skips_non_image_attachment_and_completes_protocol():
    message = SimpleNamespace(
        content="",
        raw={
            "attachments": [
                {"url": "//multimedia.nt.qq.com.cn/a.png", "filename": "a.png",
                 "content_type": "image/png"},
                {"url": "https://example.com/doc.pdf", "filename": "doc.pdf",
                 "content_type": "application/pdf"},
                {"url": "multimedia.nt.qq.com.cn/b.jpg", "filename": "b.jpg",
                 "content_type": "image/jpeg"},
            ]
        },
    )
    # 非 QQBotMessage 的轻量对象：直接给 raw，验证解析与协议补全
    refs = memes.extract_image_refs(message)

    assert refs == [
        {"url": "https://multimedia.nt.qq.com.cn/a.png", "file": "a.png"},
        {"url": "https://multimedia.nt.qq.com.cn/b.jpg", "file": "b.jpg"},
    ]


# ---------- URL 协议补全 ----------


def test_normalize_image_url_variants():
    assert memes.normalize_image_url("//multimedia.nt.qq.com.cn/a.png") == (
        "https://multimedia.nt.qq.com.cn/a.png"
    )
    assert memes.normalize_image_url("multimedia.nt.qq.com.cn/a.png") == (
        "https://multimedia.nt.qq.com.cn/a.png"
    )
    assert memes.normalize_image_url("http://example.com/a.png") == "http://example.com/a.png"
    assert memes.normalize_image_url("https://example.com/a.png") == "https://example.com/a.png"
    # 纯文件名不能当成域名补协议
    assert memes.normalize_image_url("a.png") == "a.png"
    assert memes.normalize_image_url("") == ""
