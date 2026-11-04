"""方案差异计算：新增、删除、变更、备注与未变化计数。"""

from __future__ import annotations

from datetime import date

from src.models import Assignment, DaySchedule, SchedulePlan
from src.services.comparison_service import ComparisonService
from tests.helpers import TEST_TIME


def _plan(
    plan_id: str,
    name: str,
    days: list[DaySchedule],
) -> SchedulePlan:
    return SchedulePlan(
        id=plan_id,
        name=name,
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=days,
    )


def test_compare_reports_direction_and_note_changes() -> None:
    reference = _plan(
        "plan_reference",
        "参考",
        [
            DaySchedule(
                date=date(2026, 11, 1),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early")
                ],
                note="参考备注",
            ),
            DaySchedule(
                date=date(2026, 11, 2),
                assignments=[
                    Assignment(person_id="person_b", shift_id="shift_night")
                ],
            ),
            DaySchedule(
                date=date(2026, 11, 3),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early")
                ],
            ),
        ],
    )
    current = _plan(
        "plan_current",
        "当前",
        [
            DaySchedule(
                date=date(2026, 11, 1),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early")
                ],
                note="当前备注",
            ),
            DaySchedule(
                date=date(2026, 11, 4),
                assignments=[
                    Assignment(person_id="person_b", shift_id="shift_middle")
                ],
                note="新增日期备注",
            ),
            DaySchedule(
                date=date(2026, 11, 3),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_night")
                ],
            ),
        ],
    )

    comparison = ComparisonService.compare(current, reference)

    by_date_type = {
        (item.date, item.change_type): item for item in comparison.assignment_diffs
    }
    assert comparison.added_count == 1
    assert comparison.removed_count == 1
    assert comparison.changed_count == 1
    assert comparison.unchanged_assignment_count == 1
    assert comparison.note_change_count == 2

    added = by_date_type[(date(2026, 11, 4), ComparisonService.ADDED)]
    assert added.person_id == "person_b"
    assert added.current_shift_id == "shift_middle"
    assert added.reference_shift_id is None

    removed = by_date_type[(date(2026, 11, 2), ComparisonService.REMOVED)]
    assert removed.reference_shift_id == "shift_night"
    assert removed.current_shift_id is None

    changed = by_date_type[(date(2026, 11, 3), ComparisonService.CHANGED)]
    assert changed.reference_shift_id == "shift_early"
    assert changed.current_shift_id == "shift_night"
    assert comparison.has_changes is True


def test_identical_plans_have_no_changes(base_plan: SchedulePlan) -> None:
    comparison = ComparisonService.compare(base_plan, base_plan)

    assert comparison.assignment_diffs == ()
    assert comparison.note_diffs == ()
    assert comparison.has_changes is False
    assert comparison.unchanged_assignment_count == 2
