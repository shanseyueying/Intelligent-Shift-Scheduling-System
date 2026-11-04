"""请假可用性、冲突检测与清理。"""

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
from src.services.leave_service import LeaveService
from tests.helpers import TEST_TIME


def _settings_with_leave(
    base_settings: AppSettings,
    *,
    person_id: str = "person_a",
    start: date = date(2026, 11, 3),
    end: date = date(2026, 11, 4),
) -> AppSettings:
    return ConfigService.add_leave(
        base_settings,
        person_id=person_id,
        start_date=start,
        end_date=end,
        reason="请假",
    )


def _plan_with_leave_conflict() -> SchedulePlan:
    return SchedulePlan(
        id="plan_leave_conflict",
        name="请假冲突",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 3),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early"),
                    Assignment(person_id="person_b", shift_id="shift_night"),
                ],
                note="交接设备",
            ),
            DaySchedule(
                date=date(2026, 11, 4),
                assignments=[
                    Assignment(person_id="person_b", shift_id="shift_middle")
                ],
            ),
            DaySchedule(
                date=date(2026, 11, 5),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_middle")
                ],
            ),
        ],
    )


def test_people_on_leave_includes_boundaries(
    base_settings: AppSettings,
) -> None:
    settings = _settings_with_leave(base_settings)

    assert LeaveService.is_on_leave(settings, "person_a", date(2026, 11, 3))
    assert LeaveService.is_on_leave(settings, "person_a", date(2026, 11, 4))
    assert not LeaveService.is_on_leave(settings, "person_a", date(2026, 11, 5))
    assert LeaveService.people_on_leave(settings, date(2026, 11, 3)) == {
        "person_a"
    }


def test_overlapping_leave_records_do_not_duplicate_conflict(
    base_settings: AppSettings,
) -> None:
    first = _settings_with_leave(base_settings)
    settings = _settings_with_leave(
        first,
        start=date(2026, 11, 4),
        end=date(2026, 11, 4),
    )
    plan = _plan_with_leave_conflict()

    conflicts = LeaveService.find_conflicts(settings, plan)

    assert len(conflicts) == 1
    assert conflicts[0].date == date(2026, 11, 3)


def test_leave_conflict_is_a_hard_constraint(
    base_settings: AppSettings,
) -> None:
    settings = _settings_with_leave(base_settings)
    plan = _plan_with_leave_conflict()

    violations = ConstraintService.find_hard_violations(settings, plan)

    leave_violations = [
        violation
        for violation in violations
        if violation.code == ConstraintService.LEAVE_CONFLICT
    ]
    assert len(leave_violations) == 1
    assert leave_violations[0].person_id == "person_a"
    assert leave_violations[0].date == date(2026, 11, 3)


def test_remove_conflicts_preserves_other_people_notes_and_audit(
    base_settings: AppSettings,
) -> None:
    settings = _settings_with_leave(base_settings)
    plan = _plan_with_leave_conflict()

    result = LeaveService.remove_conflicts(
        settings,
        plan,
        now=TEST_TIME + timedelta(hours=1),
    )

    assert len(result.removed) == 1
    assert [day.date for day in result.plan.days] == [
        date(2026, 11, 3),
        date(2026, 11, 4),
        date(2026, 11, 5),
    ]
    day_3 = result.plan.days[0]
    assert day_3.note == "交接设备"
    assert [assignment.person_id for assignment in day_3.assignments] == ["person_b"]
    assert result.plan.history_audit[-1].action == "leave_conflicts_removed"
    assert LeaveService.find_conflicts(settings, result.plan) == ()
    assert len(plan.days[0].assignments) == 2


def test_removing_conflict_can_drop_empty_day(
    base_settings: AppSettings,
) -> None:
    settings = _settings_with_leave(base_settings)
    plan = SchedulePlan(
        id="plan_leave_empty",
        name="清空",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 3),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early")
                ],
            )
        ],
    )

    result = LeaveService.remove_conflicts(settings, plan)

    assert result.plan.days == []
    assert len(plan.days) == 1
