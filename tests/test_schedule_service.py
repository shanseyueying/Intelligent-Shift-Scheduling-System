"""单日排班编辑服务测试。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import AppSettings, DaySchedule, SchedulePlan
from src.services.schedule_service import ScheduleService, ScheduleServiceError
from tests.helpers import TEST_TIME


def test_build_day_maps_rest_to_absent_assignment(
    base_settings: AppSettings,
) -> None:
    day = ScheduleService.build_day(
        base_settings,
        date(2026, 11, 10),
        {"person_a": "shift_early", "person_b": None},
        "早班交接钥匙",
    )

    assert day.date == date(2026, 11, 10)
    assert [(item.person_id, item.shift_id) for item in day.assignments] == [
        ("person_a", "shift_early")
    ]
    assert day.note == "早班交接钥匙"


def test_build_day_persists_only_explicit_rest_people(
    base_settings: AppSettings,
) -> None:
    day = ScheduleService.build_day(
        base_settings,
        date(2026, 11, 10),
        {"person_a": None, "person_b": None},
        "",
        rest_person_ids=["person_a"],
    )

    assert day.assignments == []
    assert day.rest_person_ids == ["person_a"]


def test_build_day_rejects_unknown_rest_and_assignment_overlap(
    base_settings: AppSettings,
) -> None:
    with pytest.raises(ScheduleServiceError, match="休息人员引用不存在"):
        ScheduleService.build_day(
            base_settings,
            date(2026, 11, 10),
            {"person_a": None, "person_b": None},
            "",
            rest_person_ids=["person_missing"],
        )

    with pytest.raises(ScheduleServiceError, match="同时排班和休息"):
        ScheduleService.build_day(
            base_settings,
            date(2026, 11, 10),
            {"person_a": "shift_early", "person_b": None},
            "",
            rest_person_ids=["person_a"],
        )


def test_build_day_requires_every_person(base_settings: AppSettings) -> None:
    with pytest.raises(ScheduleServiceError, match="缺少人员"):
        ScheduleService.build_day(
            base_settings,
            date(2026, 11, 10),
            {"person_a": "shift_early"},
            "",
        )


def test_build_day_rejects_unknown_shift(base_settings: AppSettings) -> None:
    with pytest.raises(ScheduleServiceError, match="不存在的班次"):
        ScheduleService.build_day(
            base_settings,
            date(2026, 11, 10),
            {"person_a": "shift_missing", "person_b": None},
            "",
        )


def test_build_day_rejects_overlong_note(base_settings: AppSettings) -> None:
    with pytest.raises(ScheduleServiceError, match="校验失败"):
        ScheduleService.build_day(
            base_settings,
            date(2026, 11, 10),
            {"person_a": None, "person_b": None},
            "备注" * 600,
        )


def test_upsert_day_adds_sorted_day_without_mutating_source(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    new_day = ScheduleService.build_day(
        base_settings,
        date(2026, 11, 1),
        {"person_a": "shift_early", "person_b": None},
        "",
    )
    updated = ScheduleService.upsert_day(
        base_plan,
        new_day,
        now=TEST_TIME + timedelta(hours=1),
    )

    assert [day.date for day in updated.days] == [
        date(2026, 11, 1),
        date(2026, 11, 2),
    ]
    assert len(base_plan.days) == 1
    assert updated.updated_at == TEST_TIME + timedelta(hours=1)


def test_upsert_empty_day_removes_it(base_plan: SchedulePlan) -> None:
    empty_day = DaySchedule(date=date(2026, 11, 2))
    updated = ScheduleService.upsert_day(base_plan, empty_day)

    assert updated.days == []
    assert len(base_plan.days) == 1


def test_upsert_rest_only_day_is_retained(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_rest_only",
        name="休息方案",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )
    rest_day = ScheduleService.build_day(
        base_settings,
        date(2026, 11, 10),
        {"person_a": None, "person_b": None},
        "",
        rest_person_ids=["person_b"],
    )
    updated = ScheduleService.upsert_day(plan, rest_day)

    assert len(updated.days) == 1
    assert updated.days[0].rest_person_ids == ["person_b"]


def test_upsert_note_only_day_is_retained(base_settings: AppSettings) -> None:
    plan = SchedulePlan(
        id="plan_note",
        name="备注方案",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )
    note_day = ScheduleService.build_day(
        base_settings,
        date(2026, 11, 10),
        {"person_a": None, "person_b": None},
        "设备检修，暂停排班",
    )
    updated = ScheduleService.upsert_day(plan, note_day)

    assert len(updated.days) == 1
    assert updated.days[0].assignments == []
    assert updated.days[0].note == "设备检修，暂停排班"
