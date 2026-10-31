"""权限与文本解析（纯函数）离线测试。

对应修复点：权限默认全拒绝 + 白名单放行；身份只认事件字段。
"""

from utils import (
    can_publish,
    contains_link,
    extract_url,
    is_allowed_group,
    is_publish_command,
    strip_at,
)


def test_strip_at_removes_at_fragment():
    assert strip_at("[CQ:at,qq=123456] 你好") == "你好"
    assert strip_at("<@!123456> 你好") == "你好"
    assert strip_at("<@123456>发布 https://a.com/b") == "发布 https://a.com/b"
    assert strip_at("  没有 @ 的文本  ") == "没有 @ 的文本"


def test_extract_url():
    assert extract_url("看这个 https://example.com/contest?a=1") == "https://example.com/contest?a=1"
    assert extract_url("http://a.com") == "http://a.com"
    assert extract_url("没有链接") is None


def test_contains_link():
    assert contains_link("见 https://x.y") is True
    assert contains_link("见 www.x.y") is False  # 未加协议不算
    assert contains_link("普通提问") is False


def test_is_publish_command():
    assert is_publish_command("发布 https://a.com") is True
    assert is_publish_command("发布 比赛信息文本") is True
    assert is_publish_command("发个链接") is False
    assert is_publish_command("这个比赛怎么报名", cmd="发布") is False


def test_is_allowed_group_default_deny():
    # 未配置白名单 = 不服务任何群
    assert is_allowed_group("g1", set()) is False
    assert is_allowed_group("g1", {"g2"}) is False
    assert is_allowed_group(None, {"g1"}) is False
    assert is_allowed_group("", {"g1"}) is False


def test_is_allowed_group_whitelist_pass():
    assert is_allowed_group("g1", {"g1", "g2"}) is True


def test_can_publish_default_deny():
    # 未配置白名单 = 不允许任何人发布
    assert can_publish("m1", set()) is False
    assert can_publish(None, {"m1"}) is False
    assert can_publish("", {"m1"}) is False


def test_can_publish_whitelist_pass():
    assert can_publish("m1", {"m1"}) is True
    assert can_publish("m2", {"m1"}) is False
