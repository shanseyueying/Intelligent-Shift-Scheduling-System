"""单条硬约束修复：换班优先、替补兜底与不新增硬约束。"""

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
from src.services.repair_service import RepairService
from tests.helpers import TEST_TIME


def _plan(days: list[DaySchedule], plan_id: str = "plan_repair") -> SchedulePlan:
    return SchedulePlan(
        id=plan_id,
        name="修复测试",
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


def _selected_violation(settings: AppSettings, plan: SchedulePlan):
    violations = ConstraintService.find_hard_violations(settings, plan)
    return next(
        violation
        for violation in violations
        if violation.code == ConstraintService.NIGHT_THEN_EARLY
    )


def test_swap_is_preferred_over_substitute(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.add_person(
        base_settings,
        name="王五",
        preferred_shift_ids=[],
    )
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(
                2,
                [
                    ("person_a", "shift_early"),
                    ("person_b", "shift_middle"),
                ],
            ),
        ]
    )
    violation = _selected_violation(settings, plan)

    result = RepairService.propose(
        settings,
        plan,
        violation,
        now=TEST_TIME + timedelta(hours=1),
    )

    assert result.recommendation is not None
    recommendation = result.recommendation
    assert recommendation.operation == RepairService.SWAP
    assert recommendation.hard_before == 1
    assert recommendation.hard_after == 0
    assert ConstraintService.find_hard_violations(
        settings, recommendation.candidate_plan
    ) == ()
    day_2 = next(
        day for day in recommendation.candidate_plan.days if day.date == date(2026, 11, 2)
    )
    assert {
        (assignment.person_id, assignment.shift_id)
        for assignment in day_2.assignments
    } == {
        ("person_a", "shift_middle"),
        ("person_b", "shift_early"),
    }
    assert recommendation.added_changes
    assert recommendation.removed_changes
    assert recommendation.candidate_plan.history_audit[-1].action == (
        "hard_violation_repaired"
    )
    assert plan.days[1].assignments


def test_substitute_is_used_when_no_swap_exists(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(2, [("person_a", "shift_early")]),
        ]
    )
    violation = _selected_violation(base_settings, plan)

    result = RepairService.propose(base_settings, plan, violation)

    assert result.recommendation is not None
    recommendation = result.recommendation
    assert recommendation.operation == RepairService.SUBSTITUTE
    assert recommendation.hard_after == 0
    day_2 = next(
        day for day in recommendation.candidate_plan.days if day.date == date(2026, 11, 2)
    )
    assert {
        (assignment.person_id, assignment.shift_id)
        for assignment in day_2.assignments
    } == {("person_b", "shift_early")}
    assert any(
        change.person_id == "person_b" and change.action == "added"
        for change in recommendation.changes
    )


def test_substitute_does_not_assign_a_person_who_is_explicitly_resting(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            DaySchedule(
                date=date(2026, 11, 2),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early"),
                ],
                rest_person_ids=["person_b"],
            ),
        ]
    )
    violation = _selected_violation(base_settings, plan)

    result = RepairService.propose(base_settings, plan, violation)

    assert result.recommendation is None
    assert "没有找到" in result.reason


def test_repair_rejects_candidate_that_introduces_new_hard_constraint(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(
                1,
                [
                    ("person_a", "shift_night"),
                    ("person_b", "shift_night"),
                ],
            ),
            _day(
                2,
                [
                    ("person_a", "shift_early"),
                    ("person_b", "shift_middle"),
                ],
            ),
        ]
    )
    violation = _selected_violation(base_settings, plan)
    before_count = len(ConstraintService.find_hard_violations(base_settings, plan))

    result = RepairService.propose(base_settings, plan, violation)

    assert before_count == 1
    assert result.recommendation is None
    assert "没有找到" in result.reason


def test_missing_violation_returns_clear_reason(
    base_settings: AppSettings,
) -> None:
    plan = _plan([_day(1, [("person_a", "shift_early")])])
    stale_violation = _selected_violation(
        base_settings,
        _plan(
            [
                _day(1, [("person_a", "shift_night")]),
                _day(2, [("person_a", "shift_early")]),
            ]
        ),
    )

    result = RepairService.propose(base_settings, plan, stale_violation)

    assert result.recommendation is None
    assert "已不存在" in result.reason
