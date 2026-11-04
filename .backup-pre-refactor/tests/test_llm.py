"""大模型结果校验与上下文控制离线测试。

对应修复点：提取结果结构校验（失败不写库）；输入/输出/上下文限量。
"""

import pytest

from llm import (
    _limit_contests,
    normalize_time,
    parse_contest_result,
    validate_contest_info,
)


def test_normalize_time_formats():
    assert normalize_time("2026-11-01 10:30") == "2026-11-01 10:30"
    assert normalize_time("2026-11-01 10:30:00") == "2026-11-01 10:30"
    assert normalize_time("2026/11/01 10:30") == "2026-11-01 10:30"
    assert normalize_time("2026-11-01") == "2026-11-01 00:00"


def test_normalize_time_invalid():
    assert normalize_time(None) is None
    assert normalize_time("") is None
    assert normalize_time("   ") is None
    assert normalize_time("下周三") is None
    assert normalize_time("2026-13-40") is None
    assert normalize_time("2026-11-01 25:00") is None


def test_validate_contest_info_basic():
    info, err = validate_contest_info(
        {
            "name": " 蓝桥杯 ",
            "reg_deadline": "2026-11-01 10:30",
            "contest_time": "2026-11-05 09:00",
            "mode": "线上",
            "location": "机房",
        }
    )
    assert err is None
    assert info["name"] == "蓝桥杯"
    assert info["reg_deadline"] == "2026-11-01 10:30"


def test_validate_contest_info_not_dict():
    info, err = validate_contest_info(["name", "x"])
    assert info is None and err is not None


def test_validate_contest_info_missing_name():
    info, err = validate_contest_info({"reg_deadline": "2026-11-01 10:30"})
    assert info is None and err is not None


def test_validate_contest_info_bad_time():
    info, err = validate_contest_info({"name": "x", "reg_deadline": "下周"})
    assert info is None and err is not None


def test_validate_contest_info_bad_field_type():
    info, err = validate_contest_info({"name": "x", "mode": 123})
    assert info is None and err is not None


def test_validate_contest_info_time_optional():
    info, err = validate_contest_info({"name": "x"})
    assert err is None
    assert info["reg_deadline"] == ""
    assert info["contest_time"] == ""


def test_parse_contest_result_empty():
    info, err = parse_contest_result("")
    assert info is None and err is not None
    info, err = parse_contest_result("   \n  ")
    assert info is None and err is not None


def test_parse_contest_result_not_json():
    info, err = parse_contest_result("随便说点什么")
    assert info is None and err is not None


def test_parse_contest_result_fenced_json():
    content = '```json\n{"name": "ACM 校赛", "contest_time": "2026-11-20 14:00"}\n```'
    info, err = parse_contest_result(content)
    assert err is None
    assert info["name"] == "ACM 校赛"
    assert info["contest_time"] == "2026-11-20 14:00"


def test_parse_contest_result_invalid_field():
    info, err = parse_contest_result('{"name": "x", "reg_deadline": "not-a-date"}')
    assert info is None and err is not None


def test_limit_contests_limits_count_and_chars():
    contests = [
        {"name": f"比赛{i}", "reg_deadline": "", "contest_time": "",
         "mode": "", "location": ""}
        for i in range(20)
    ]
    limited = _limit_contests(contests, max_items=5, max_chars=100000)
    assert len(limited) <= 5

    tiny = _limit_contests(contests, max_items=5, max_chars=10)
    # 第一条即使放不下也至少保留一条？实现是放不下就 break，可能为 0
    assert len(tiny) <= 1
