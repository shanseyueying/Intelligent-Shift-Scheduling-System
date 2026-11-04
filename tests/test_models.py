"""模型层：保证脏数据在入口处失败，而不是进入算法后产生错误结果。"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from src.models import (
    AppSettings,
    Assignment,
    CoverageGroup,
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


def test_day_schedule_deduplicates_rest_and_rejects_shift_overlap() -> None:
    day = DaySchedule(
        date=date(2026, 11, 2),
        rest_person_ids=["person_a", "person_a", "person_b"],
    )
    assert day.rest_person_ids == ["person_a", "person_b"]

    with pytest.raises(ValidationError, match="同时排班和休息"):
        DaySchedule(
            date=date(2026, 11, 2),
            assignments=[
                Assignment(person_id="person_a", shift_id="shift_early"),
            ],
            rest_person_ids=["person_a"],
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


def test_coverage_group_deduplicates_shifts_and_validates_name() -> None:
    group = CoverageGroup(
        name=" 日班组 ",
        shift_ids=["shift_early", "shift_middle", "shift_early"],
    )
    assert group.name == "日班组"
    assert group.shift_ids == ["shift_early", "shift_middle"]

    with pytest.raises(ValidationError, match="不能为空"):
        CoverageGroup(name="  ", shift_ids=["shift_early"])
    with pytest.raises(ValidationError):
        CoverageGroup(name="空组", shift_ids=[])


def test_settings_rejects_shift_in_two_active_coverage_groups(
    shifts: list[Shift],
) -> None:
    groups = [
        CoverageGroup(
            id="coverage_day",
            name="日班组",
            shift_ids=["shift_early", "shift_middle"],
        ),
        CoverageGroup(
            id="coverage_mixed",
            name="混合组",
            shift_ids=["shift_middle"],
        ),
    ]
    with pytest.raises(ValidationError, match="多个启用中的覆盖组"):
        AppSettings(shifts=shifts, coverage_groups=groups)


def test_settings_allows_shift_overlap_with_an_inactive_group(
    shifts: list[Shift],
) -> None:
    groups = [
        CoverageGroup(
            id="coverage_old",
            name="旧日班组",
            shift_ids=["shift_early"],
            is_active=False,
        ),
        CoverageGroup(
            id="coverage_new",
            name="新日班组",
            shift_ids=["shift_early"],
        ),
    ]
    settings = AppSettings(shifts=shifts, coverage_groups=groups)
    assert {group.name for group in settings.coverage_groups} == {
        "旧日班组",
        "新日班组",
    }


def test_settings_rejects_coverage_group_with_missing_shift(
    shifts: list[Shift],
) -> None:
    group = CoverageGroup(
        id="coverage_bad",
        name="坏组",
        shift_ids=["shift_missing"],
    )
    with pytest.raises(ValidationError, match="不存在的班次"):
        AppSettings(shifts=shifts, coverage_groups=[group])


def test_settings_rejects_duplicate_coverage_group_names(
    shifts: list[Shift],
) -> None:
    groups = [
        CoverageGroup(name="日班组", shift_ids=["shift_early"]),
        CoverageGroup(name="日班组", shift_ids=["shift_middle"]),
    ]
    with pytest.raises(ValidationError, match="覆盖组名称重复"):
        AppSettings(shifts=shifts, coverage_groups=groups)


def test_settings_rejects_unknown_json_field() -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"people": [], "shifts": [], "unexpected": 1})
