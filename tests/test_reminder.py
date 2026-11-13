"""提醒时间解析（legacy contests 链已下线，仅保留通用解析用例）。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cdutetc import reminder
from cdutetc.config import TZ_NAME

TZ = ZoneInfo(TZ_NAME)


def test_parse_time_uses_beijing_tz():
    dt = reminder._parse_time("2026-11-01 10:30")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)
    assert reminder._parse_time("") is None
    assert reminder._parse_time("下周三") is None
