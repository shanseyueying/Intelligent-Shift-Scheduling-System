"""夜班后次日早班硬约束测试。"""

from __future__ import annotations

from datetime import date

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    SchedulePlan,
)
from src.services.constraint_service import ConstraintService
from tests.helpers import TEST_TIME


def _plan_with_days(*days: DaySchedule) -> SchedulePlan:
    return SchedulePlan(
        id="plan_constraint",
        name="约束测试",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=list(days),
    )


def test_night_then_next_day_early_is_violation(
    base_settings: AppSettings,
) -> None:
    plan = _plan_with_days(
        DaySchedule(
            date=date(2026, 11, 3),
            assignments=[Assignment(person_id="person_a", shift_id="shift_night")],
        ),
        DaySchedule(
            date=date(2026, 11, 4),
            assignments=[Assignment(person_id="person_a", shift_id="shift_early")],
        ),
    )

    violations = ConstraintService.find_hard_violations(base_settings, plan)

    assert len(violations) == 1
    assert violations[0].code == ConstraintService.NIGHT_THEN_EARLY
    assert violations[0].person_id == "person_a"
    assert violations[0].date == date(2026, 11, 4)
    assert violations[0].related_date == date(2026, 11, 3)


def test_night_then_early_after_a_gap_is_allowed(
    base_settings: AppSettings,
) -> None:
    plan = _plan_with_days(
        DaySchedule(
            date=date(2026, 11, 3),
            assignments=[Assignment(person_id="person_a", shift_id="shift_night")],
        ),
        DaySchedule(
            date=date(2026, 11, 5),
            assignments=[Assignment(person_id="person_a", shift_id="shift_early")],
        ),
    )

    assert ConstraintService.find_hard_violations(base_settings, plan) == ()


def test_night_then_early_across_different_people_is_allowed(
    base_settings: AppSettings,
) -> None:
    plan = _plan_with_days(
        DaySchedule(
            date=date(2026, 11, 3),
            assignments=[Assignment(person_id="person_a", shift_id="shift_night")],
        ),
        DaySchedule(
            date=date(2026, 11, 4),
            assignments=[Assignment(person_id="person_b", shift_id="shift_early")],
        ),
    )

    assert ConstraintService.find_hard_violations(base_settings, plan) == ()


def test_new_violations_only_returns_new_keys(
    base_settings: AppSettings,
) -> None:
    before_plan = _plan_with_days(
        DaySchedule(
            date=date(2026, 11, 3),
            assignments=[Assignment(person_id="person_a", shift_id="shift_night")],
        ),
        DaySchedule(
            date=date(2026, 11, 4),
            assignments=[Assignment(person_id="person_a", shift_id="shift_early")],
        ),
    )
    after_plan = _plan_with_days(
        *before_plan.days,
        DaySchedule(
            date=date(2026, 11, 5),
            assignments=[Assignment(person_id="person_a", shift_id="shift_night")],
        ),
        DaySchedule(
            date=date(2026, 11, 6),
            assignments=[Assignment(person_id="person_a", shift_id="shift_early")],
        ),
    )

    before = ConstraintService.find_hard_violations(base_settings, before_plan)
    after = ConstraintService.find_hard_violations(base_settings, after_plan)
    new = ConstraintService.new_violations(before, after)

    assert len(before) == 1
    assert len(after) == 2
    assert len(new) == 1
    assert new[0].related_date == date(2026, 11, 5)
    assert new[0].date == date(2026, 11, 6)
