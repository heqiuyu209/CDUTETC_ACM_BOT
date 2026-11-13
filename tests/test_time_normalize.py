"""规格 A：时间表达式的归一化与拆分。

覆盖用户真实输入的脏时间写法：``19-21点``、``晚上7点到9点``、``19点半-21点``、
``星期四晚上7点半到9点`` 等，统一归一化为 ``HH:MM`` / ``HH:MM-HH:MM`` 并带上星期。
"""

import pytest

from cdutetc.utils import normalize_time_expression, split_normalized_time


@pytest.mark.parametrize("raw, expected", [
    ("19-21点", "19:00-21:00"),
    ("19点-21点", "19:00-21:00"),
    ("19点30分-21点", "19:30-21:00"),
    ("19点半-21点", "19:30-21:00"),
    ("晚上7点到9点", "19:00-21:00"),
    ("十九点-二十一点", "19:00-21:00"),
])
def test_dirty_range_is_normalized_to_clock_span(raw, expected):
    """时段写法（点/半/中文数字/到）统一成 ``HH:MM-HH:MM``。"""
    assert normalize_time_expression(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("7点", "07:00"),
    ("下午两点半", "14:30"),
    ("中午12点", "12:00"),
])
def test_single_clock_is_normalized(raw, expected):
    """单点时间按口语时段（下午/中午/半）换算成 24 小时制。"""
    assert normalize_time_expression(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("周四 19-21点", "周四 19:00-21:00"),
    ("每周四 19:00-21:00", "周四 19:00-21:00"),
    ("星期天晚上7点到9点", "周日 19:00-21:00"),
    ("周日 19～21点", "周日 19:00-21:00"),
])
def test_weekday_is_normalized_and_kept(raw, expected):
    """星期写法统一成「周X」，并与时段一起返回。"""
    assert normalize_time_expression(raw) == expected


def test_split_normalized_time_separates_weekday_start_end():
    """拆分结果固定为 (星期, 开始, 结束) 三元组。"""
    assert split_normalized_time("周四 19-21点") == ("周四", "19:00", "21:00")
    assert split_normalized_time("星期四晚上7点半到9点") == ("周四", "19:30", "21:00")


def test_split_normalized_time_without_range_has_empty_end():
    assert split_normalized_time("周三 19点") == ("周三", "19:00", "")


@pytest.mark.parametrize("raw", ["时间待定", "图论进阶", ""])
def test_unrecognized_text_is_returned_unchanged(raw):
    """识别不出时间时原样返回，绝不臆造一个时间。"""
    assert normalize_time_expression(raw) == raw
    assert split_normalized_time(raw) == ("", "", "")


def test_blank_input_is_normalized_to_empty():
    """纯空白输入按空串处理，调用方据此判「没有时间」。"""
    assert normalize_time_expression("   ") == ""
    assert split_normalized_time("   ") == ("", "", "")


def test_cross_day_range_keeps_both_clocks():
    """跨零点时段（23:00-01:00）两个时刻都要保留，交给调用方判跨天。"""
    assert split_normalized_time("23:00-01:00") == ("", "23:00", "01:00")
