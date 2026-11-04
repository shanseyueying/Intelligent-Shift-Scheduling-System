"""健康分：连续同班次、负荷均衡、偏好满足与硬约束否决。"""

from __future__ import annotations

from datetime import date, timedelta

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    SchedulePlan,
)
from src.services.config_service import ConfigService
from src.services.constraint_service import ConstraintService
from src.services.scoring_service import ScoringService
from tests.helpers import TEST_TIME


def _plan(days: list[DaySchedule], plan_id: str = "plan_score") -> SchedulePlan:
    return SchedulePlan(
        id=plan_id,
        name="评分测试",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=days,
    )


def _day(day_number: int, assignments: list[tuple[str, str]]) -> DaySchedule:
    return DaySchedule(
        date=date(2026, 11, day_number),
        assignments=[
            Assignment(person_id=person_id, shift_id=shift_id)
            for person_id, shift_id in assignments
        ],
    )


def test_empty_plan_has_no_score(base_settings: AppSettings) -> None:
    result = ScoringService.evaluate(
        base_settings,
        _plan([], plan_id="plan_score_empty"),
    )

    assert result.has_data is False
    assert result.score is None
    assert result.status == ScoringService.STATUS_NO_DATA
    assert result.soft.total_penalty == 0


def test_three_or_more_same_shift_days_in_a_row_are_penalized(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_early")]),
            _day(2, [("person_a", "shift_early")]),
            _day(3, [("person_a", "shift_early")]),
        ],
        plan_id="plan_score_continuity",
    )

    result = ScoringService.evaluate(base_settings, plan)

    assert result.soft.continuity_extra_days == 1
    assert result.soft.continuity_penalty == 2
    assert result.soft.load_penalty == 0
    assert result.soft.preference_penalty == 0
    assert result.score == 98
    assert result.status == ScoringService.STATUS_HEALTHY


def test_two_consecutive_same_shift_days_are_not_penalized(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_early")]),
            _day(2, [("person_a", "shift_early")]),
        ],
        plan_id="plan_score_two_days",
    )

    result = ScoringService.evaluate(base_settings, plan)

    assert result.soft.continuity_extra_days == 0
    assert result.soft.continuity_penalty == 0
    assert result.score == 100


def test_load_spread_and_satisfied_preferences(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [_day(1, [("person_a", "shift_early"), ("person_b", "shift_night")])],
        plan_id="plan_score_load",
    )

    result = ScoringService.evaluate(base_settings, plan)

    assert result.soft.load_spread == 0.6
    assert result.soft.load_penalty == 3
    assert result.soft.preference_total == 2
    assert result.soft.preference_satisfied == 2
    assert result.soft.preference_penalty == 0
    assert result.score == 97


def test_unmet_preference_penalty(base_settings: AppSettings) -> None:
    settings = ConfigService.update_person(
        base_settings,
        "person_b",
        name="李四",
        preferred_shift_ids=[],
    )
    plan = _plan(
        [_day(1, [("person_a", "shift_middle"), ("person_b", "shift_middle")])],
        plan_id="plan_score_preference",
    )

    result = ScoringService.evaluate(settings, plan)

    assert result.soft.preference_total == 1
    assert result.soft.preference_satisfied == 0
    assert result.soft.preference_penalty == 20
    assert result.soft.load_spread == 0
    assert result.score == 80
    assert result.status == ScoringService.STATUS_ATTENTION


def test_hard_violation_overrides_soft_score(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(2, [("person_a", "shift_early")]),
        ],
        plan_id="plan_score_hard",
    )

    result = ScoringService.evaluate(base_settings, plan)

    assert len(result.hard_violations) == 1
    assert result.hard_violations[0].code == ConstraintService.NIGHT_THEN_EARLY
    assert result.score == 0
    assert result.status == ScoringService.STATUS_FAILED


def test_leave_conflict_also_overrides_score(base_settings: AppSettings) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        reason="事假",
    )
    plan = _plan(
        [_day(1, [("person_a", "shift_early")])],
        plan_id="plan_score_leave",
    )

    result = ScoringService.evaluate(settings, plan)

    assert result.score == 0
    assert result.status == ScoringService.STATUS_FAILED
    assert any(
        violation.code == ConstraintService.LEAVE_CONFLICT
        for violation in result.hard_violations
    )


def test_long_run_continuity_is_capped(base_settings: AppSettings) -> None:
    days = [
        _day(day, [("person_a", "shift_early")])
        for day in range(1, 15)
    ]
    plan = SchedulePlan(
        id="plan_score_cap",
        name="连续上限",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 10, 20) + timedelta(days=index),
                assignments=day.assignments,
            )
            for index, day in enumerate(days)
        ],
    )

    result = ScoringService.evaluate(base_settings, plan)

    assert result.soft.continuity_extra_days == 12
    assert result.soft.continuity_penalty == ScoringService.CONTINUITY_CAP
