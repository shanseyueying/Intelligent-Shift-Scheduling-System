"""测试共享数据。所有存储测试只使用 pytest 的临时目录。"""

from __future__ import annotations

from datetime import date

import pytest

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    Person,
    SchedulePlan,
    Shift,
)
from tests.helpers import TEST_TIME


@pytest.fixture
def shifts() -> list[Shift]:
    return [
        Shift(id="shift_early", name="早班", color="#F59E0B", is_early=True),
        Shift(id="shift_middle", name="中班", color="#3B82F6", load_weight=1.1),
        Shift(
            id="shift_night",
            name="夜班",
            color="#7C3AED",
            load_weight=1.6,
            is_night=True,
        ),
    ]


@pytest.fixture
def people() -> list[Person]:
    return [
        Person(id="person_a", name="张三", preferred_shift_ids=["shift_early"]),
        Person(id="person_b", name="李四", preferred_shift_ids=["shift_night"]),
    ]


@pytest.fixture
def base_settings(people: list[Person], shifts: list[Shift]) -> AppSettings:
    return AppSettings(
        people=people,
        shifts=shifts,
        leaves=[],
        active_plan_id="plan_base",
    )


@pytest.fixture
def base_plan() -> SchedulePlan:
    return SchedulePlan(
        id="plan_base",
        name="基础方案",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 2),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early"),
                    Assignment(person_id="person_b", shift_id="shift_night"),
                ],
                note="夜班交接设备钥匙",
            )
        ],
    )
