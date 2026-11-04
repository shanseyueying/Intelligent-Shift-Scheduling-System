"""模型层：保证脏数据在入口处失败，而不是进入算法后产生错误结果。"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    LeaveRecord,
    Person,
    SchedulePlan,
    Shift,
)


def test_shift_rejects_early_and_night_flags_together() -> None:
    with pytest.raises(ValidationError, match="不能同时标记"):
        Shift(name="异常班", is_early=True, is_night=True)


def test_shift_normalizes_color_and_validates_weight() -> None:
    shift = Shift(name="测试班", color="#a1b2c3")
    assert shift.color == "#A1B2C3"

    with pytest.raises(ValidationError):
        Shift(name="零负荷", load_weight=0)


def test_person_deduplicates_preferences_in_order() -> None:
    person = Person(
        name="王五",
        preferred_shift_ids=["shift_b", "shift_a", "shift_b"],
    )
    assert person.preferred_shift_ids == ["shift_b", "shift_a"]


def test_leave_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="结束日期不能早于"):
        LeaveRecord(
            person_id="person_a",
            start_date=date(2026, 11, 5),
            end_date=date(2026, 11, 3),
        )


def test_settings_rejects_missing_preference_target(
    people: list[Person],
    shifts: list[Shift],
) -> None:
    invalid_person = Person(
        id="person_c",
        name="赵六",
        preferred_shift_ids=["shift_missing"],
    )
    with pytest.raises(ValidationError, match="不存在的偏好班次"):
        AppSettings(people=[*people, invalid_person], shifts=shifts)


def test_settings_rejects_duplicate_person_names_case_insensitively(
    people: list[Person],
    shifts: list[Shift],
) -> None:
    duplicate = Person(id="person_x", name="张三")
    with pytest.raises(ValidationError, match="人员姓名重复"):
        AppSettings(people=[*people, duplicate], shifts=shifts)


def test_settings_rejects_leave_for_unknown_person(
    people: list[Person],
    shifts: list[Shift],
) -> None:
    leave = LeaveRecord(
        person_id="person_missing",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
    )
    with pytest.raises(ValidationError, match="不存在的人员"):
        AppSettings(people=people, shifts=shifts, leaves=[leave])


def test_day_schedule_rejects_duplicate_person() -> None:
    with pytest.raises(ValidationError, match="重复排班人员"):
        DaySchedule(
            date=date(2026, 11, 2),
            assignments=[
                Assignment(person_id="person_a", shift_id="shift_early"),
                Assignment(person_id="person_a", shift_id="shift_night"),
            ],
        )


def test_plan_rejects_duplicate_dates() -> None:
    first = DaySchedule(date=date(2026, 11, 2))
    second = DaySchedule(date=date(2026, 11, 2))
    with pytest.raises(ValidationError, match="重复日期"):
        SchedulePlan(name="重复日期", days=[first, second])


def test_plan_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="必须包含时区"):
        SchedulePlan(
            name="无时区",
            created_at=datetime(2026, 11, 1, 8, 0),
            updated_at=datetime(2026, 11, 1, 8, 0),
        )


def test_settings_rejects_unknown_json_field() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"people": [], "shifts": [], "unexpected": 1})
