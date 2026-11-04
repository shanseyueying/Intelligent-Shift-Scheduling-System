"""日期计算工具，供界面与算法共享。"""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import date, datetime, timedelta


def month_start(value: date) -> date:
    return value.replace(day=1)


def days_in_month(value: date) -> int:
    return calendar.monthrange(value.year, value.month)[1]


def month_end(value: date) -> date:
    return value.replace(day=days_in_month(value))


def iter_days(start: date, end: date) -> Iterator[date]:
    """包含首尾日期逐日迭代；调用方需保证 start <= end。"""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def date_range_label(start: date, end: date) -> str:
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()} 至 {end.isoformat()}"


def local_date_from_iso(value: str) -> date:
    """把组件回调的 ISO 时间还原为本地自然日。

    FullCalendar 的 all-day 点击值经 toISOString() 后可能变成前一天 16:00Z
    （UTC+8 环境）。此处统一转换到服务器本地时区，避免点 11 月 2 日却编辑
    11 月 1 日。本项目定位为本地单机应用，服务器与浏览器处于同一时区。
    """
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone().date()
