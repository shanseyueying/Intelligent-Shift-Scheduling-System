"""两两换班推荐：硬约束过滤、健康护栏、排序与审计。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    Person,
    SchedulePlan,
    Shift,
)
from src.services.config_service import ConfigService
from src.services.constraint_service import ConstraintService
from src.services.scoring_service import ScoringService
from src.services.swap_service import SwapService, SwapServiceError
from tests.helpers import TEST_TIME


def _plan(
    days: list[DaySchedule],
    plan_id: str = "plan_swap",
) -> SchedulePlan:
    return SchedulePlan(
        id=plan_id,
        name="换班测试",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=days,
    )


def _day(day_number: int, assignments: list[tuple[str, str]], note: str = "") -> DaySchedule:
    return DaySchedule(
        date=date(2026, 11, day_number),
        assignments=[
            Assignment(person_id=person_id, shift_id=shift_id)
            for person_id, shift_id in assignments
        ],
        note=note,
    )


def test_same_day_swap_improves_load_and_health(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [_day(1, [("person_a", "shift_night"), ("person_b", "shift_early")], "交接")]
    )
    current_health = ScoringService.evaluate(base_settings, plan)

    recommendations = SwapService.recommend(
        base_settings,
        plan,
        "person_a",
        now=TEST_TIME + timedelta(hours=1),
    )

    assert current_health.score == 77
    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.target_person_id == "person_a"
    assert recommendation.partner_person_id == "person_b"
    assert recommendation.target_shift_id == "shift_night"
    assert recommendation.partner_shift_id == "shift_early"
    assert recommendation.target_load_reduction == 0.6
    assert recommendation.health_before == 77
    assert recommendation.health_after == 97
    assert ConstraintService.find_hard_violations(
        base_settings, recommendation.candidate_plan
    ) == ()

    swapped_day = recommendation.candidate_plan.days[0]
    assert swapped_day.note == "交接"
    assert {
        (assignment.person_id, assignment.shift_id)
        for assignment in swapped_day.assignments
    } == {
        ("person_a", "shift_early"),
        ("person_b", "shift_night"),
    }
    assert plan.days[0].assignments[0].shift_id == "shift_night"
    assert recommendation.candidate_plan.history_audit[-1].action == "swap_applied"


def test_cross_day_swap_moves_assignments(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(2, [("person_b", "shift_early")]),
        ]
    )

    recommendations = SwapService.recommend(base_settings, plan, "person_a")

    assert len(recommendations) == 1
    candidate_days = {
        day.date: {
            (assignment.person_id, assignment.shift_id)
            for assignment in day.assignments
        }
        for day in recommendations[0].candidate_plan.days
    }
    assert candidate_days[date(2026, 11, 1)] == {("person_b", "shift_night")}
    assert candidate_days[date(2026, 11, 2)] == {("person_a", "shift_early")}


def test_health_guard_rejects_load_reduction_that_lowers_score(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.update_person(
        base_settings,
        "person_a",
        name="张三",
        preferred_shift_ids=["shift_night"],
    )
    settings = ConfigService.update_person(
        settings,
        "person_b",
        name="李四",
        preferred_shift_ids=["shift_early"],
    )
    plan = _plan(
        [_day(1, [("person_a", "shift_night"), ("person_b", "shift_early")])]
    )
    before = ScoringService.evaluate(settings, plan)

    recommendations = SwapService.recommend(settings, plan, "person_a")

    assert before.score == 97
    assert recommendations == ()


def test_leave_on_new_date_excludes_candidate(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_b",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        reason="事假",
    )
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(2, [("person_b", "shift_early")]),
        ]
    )

    assert SwapService.recommend(settings, plan, "person_a") == ()


def test_top_three_ranked_by_target_load_reduction() -> None:
    shifts = [
        Shift(id="shift_early", name="早班", load_weight=1.0, is_early=True),
        Shift(id="shift_middle", name="中班", load_weight=1.1),
        Shift(id="shift_night", name="夜班", load_weight=1.6, is_night=True),
    ]
    settings = AppSettings(
        people=[
            Person(id="person_a", name="A"),
            Person(id="person_b", name="B"),
            Person(id="person_c", name="C"),
            Person(id="person_d", name="D"),
        ],
        shifts=shifts,
    )
    plan = _plan(
        [
            _day(1, [("person_a", "shift_night")]),
            _day(2, [("person_b", "shift_early")]),
            _day(3, [("person_c", "shift_middle")]),
            _day(4, [("person_d", "shift_middle")]),
        ]
    )

    recommendations = SwapService.recommend(settings, plan, "person_a")

    assert len(recommendations) == 3
    reductions = [item.target_load_reduction for item in recommendations]
    assert reductions == sorted(reductions, reverse=True)
    assert reductions[0] == 0.6
    assert reductions[1:] == [0.5, 0.5]
    assert all(item.health_after >= item.health_before for item in recommendations)


def test_equal_weights_produce_no_candidate(
    base_settings: AppSettings,
) -> None:
    plan = _plan(
        [
            _day(1, [("person_a", "shift_early")]),
            _day(2, [("person_b", "shift_early")]),
        ]
    )

    assert SwapService.recommend(base_settings, plan, "person_a") == ()


def test_inactive_target_is_rejected(base_settings: AppSettings) -> None:
    settings = ConfigService.set_person_active(base_settings, "person_a", False)
    plan = _plan([_day(1, [("person_a", "shift_night")])])

    with pytest.raises(SwapServiceError, match="已停用"):
        SwapService.recommend(settings, plan, "person_a")
