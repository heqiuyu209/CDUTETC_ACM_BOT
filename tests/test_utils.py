"""权限与文本解析（纯函数）离线测试。

对应修复点：权限默认全拒绝 + 白名单放行；身份只认事件字段。
"""

from cdutetc import config
from cdutetc.utils import (
    contains_link,
    extract_url,
    is_allowed_group,
    is_teacher,
    is_valid_account_id,
    match_publish_command,
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


def test_match_publish_command():
    assert match_publish_command("发布 https://a.com") == "https://a.com"
    assert match_publish_command("发布 比赛信息文本") == "比赛信息文本"
    assert match_publish_command("发个链接") is None
    assert match_publish_command("这个比赛怎么报名", cmd="发布") is None


def test_is_allowed_group_default_deny():
    # 未配置白名单 = 不服务任何群
    assert is_allowed_group("g1", set()) is False
    assert is_allowed_group("g1", {"g2"}) is False
    assert is_allowed_group(None, {"g1"}) is False
    assert is_allowed_group("", {"g1"}) is False


def test_is_allowed_group_whitelist_pass():
    assert is_allowed_group("g1", {"g1", "g2"}) is True


def test_is_teacher_default_deny(monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    assert is_teacher("m1") is False
    assert is_teacher(None) is False
    assert is_teacher("") is False


def test_is_teacher_whitelist_pass(monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"m1"})
    assert is_teacher("m1") is True
    assert is_teacher("m2") is False


def test_publish_command_accepts_chinese_punctuation():
    """中文输入法下「/添加比赛，xxx」不带空格也要能识别。"""
    assert match_publish_command("/添加比赛，15分钟过后参加蓝桥杯") == "15分钟过后参加蓝桥杯"
    assert match_publish_command("添加比赛：今晚八点训练赛") == "今晚八点训练赛"
    assert match_publish_command("/发布，蓝桥杯下周开始") == "蓝桥杯下周开始"


def test_publish_command_rejects_question_like_text():
    """带命令词但其实是提问的，不能被当成发布。"""
    assert match_publish_command("发布什么比赛") is None
    assert match_publish_command("添加比赛信息怎么弄") is None


def test_valid_account_id_accepts_openid():
    """官方机器人的 openid 是十六进制混排，不能按纯数字拒绝。"""
    assert is_valid_account_id("A0EF5D2DFBA4B3CC76E3C70C47971FDE")
    assert is_valid_account_id("DCF4F26867BD71753336F962C626F3FD")


def test_valid_account_id_accepts_qq_number():
    assert is_valid_account_id("585830366")
    assert is_valid_account_id("2097187372")


def test_valid_account_id_rejects_bad_input():
    assert not is_valid_account_id("")
    assert not is_valid_account_id("   ")
    assert not is_valid_account_id("带 空格")
    assert not is_valid_account_id("x" * 70)
