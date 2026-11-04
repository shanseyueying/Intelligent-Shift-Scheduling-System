"""软归档下，单日编辑视图对历史排班的保护。"""

from __future__ import annotations

from datetime import date

import pytest

from src.models import AppSettings, Assignment, DaySchedule, SchedulePlan
from src.services.config_service import ConfigService
from src.services.schedule_service import ScheduleService, ScheduleServiceError
from tests.helpers import TEST_TIME


def test_archived_person_assignment_is_preserved_and_not_editable(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    archived = ConfigService.set_person_active(base_settings, "person_b", False)

    view = ScheduleService.build_edit_view(
        archived,
        base_plan,
        date(2026, 11, 2),
    )

    assert [person.id for person in view.editable_people] == ["person_a"]
    assert [assignment.person_id for assignment in view.preserved_assignments] == [
        "person_b"
    ]
    merged = view.merge_selections({"person_a": None})
    assert merged["person_b"] == "shift_night"
    assert merged["person_a"] is None


def test_assignment_with_archived_shift_is_preserved(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    archived = ConfigService.set_shift_active(base_settings, "shift_night", False)

    view = ScheduleService.build_edit_view(
        archived,
        base_plan,
        date(2026, 11, 2),
    )

    assert [person.id for person in view.editable_people] == ["person_a"]
    assert {shift.id for shift in view.active_shifts} == {
        "shift_early",
        "shift_middle",
    }
    assert [assignment.person_id for assignment in view.preserved_assignments] == [
        "person_b"
    ]

    with pytest.raises(ScheduleServiceError, match="启用中的班次"):
        view.merge_selections({"person_a": "shift_night"})


def test_archived_person_without_assignment_is_hidden(
    base_settings: AppSettings,
) -> None:
    archived = ConfigService.set_person_active(base_settings, "person_b", False)
    plan = SchedulePlan(
        id="plan_edit_view",
        name="编辑视图",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )

    view = ScheduleService.build_edit_view(archived, plan, date(2026, 11, 5))
    merged = view.merge_selections({"person_a": "shift_early"})

    assert merged == {"person_a": "shift_early", "person_b": None}


def test_explicit_rest_is_editable_and_archived_rest_is_preserved(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_rest_edit",
        name="休息编辑",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 5),
                rest_person_ids=["person_a", "person_b"],
            )
        ],
    )
    archived = ConfigService.set_person_active(base_settings, "person_b", False)

    view = ScheduleService.build_edit_view(archived, plan, date(2026, 11, 5))

    assert [person.id for person in view.editable_people] == ["person_a"]
    assert view.rest_person_ids == ("person_a",)
    assert view.preserved_rest_person_ids == ("person_b",)
    assert view.initial_shift_by_person == {"person_a": None}


def test_unknown_rest_person_blocks_editing(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_unknown_rest",
        name="脏休息引用",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 5),
                rest_person_ids=["person_missing"],
            )
        ],
    )

    with pytest.raises(ScheduleServiceError, match="未知休息人员"):
        ScheduleService.build_edit_view(
            base_settings,
            plan,
            date(2026, 11, 5),
        )


def test_unknown_references_block_editing(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_unknown",
        name="脏引用",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 5),
                assignments=[
                    Assignment(person_id="person_missing", shift_id="shift_early")
                ],
            )
        ],
    )

    with pytest.raises(ScheduleServiceError, match="未知人员"):
        ScheduleService.build_edit_view(
            base_settings,
            plan,
            date(2026, 11, 5),
        )


def test_leave_person_assignment_is_preserved_and_not_editable(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 2),
        reason="事假",
    )

    view = ScheduleService.build_edit_view(
        settings,
        base_plan,
        date(2026, 11, 2),
    )

    assert [person.id for person in view.on_leave_people] == ["person_a"]
    assert [person.id for person in view.editable_people] == ["person_b"]
    assert [a.person_id for a in view.preserved_assignments] == ["person_a"]
    merged = view.merge_selections({"person_b": "shift_middle"})
    assert merged == {
        "person_a": "shift_early",
        "person_b": "shift_middle",
    }


def test_leave_person_without_assignment_is_hidden(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 11, 8),
        end_date=date(2026, 11, 8),
        reason="事假",
    )
    plan = SchedulePlan(
        id="plan_leave_hidden",
        name="请假隐藏",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )

    view = ScheduleService.build_edit_view(settings, plan, date(2026, 11, 8))

    assert [person.id for person in view.editable_people] == ["person_b"]
    assert [person.id for person in view.on_leave_people] == ["person_a"]
    assert view.preserved_assignments == ()


def test_all_people_archived_still_allows_note_edit(
    base_settings: AppSettings,
) -> None:
    archived_a = ConfigService.set_person_active(base_settings, "person_a", False)
    archived = ConfigService.set_person_active(archived_a, "person_b", False)
    plan = SchedulePlan(
        id="plan_all_archived",
        name="全员停用",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )

    view = ScheduleService.build_edit_view(archived, plan, date(2026, 11, 5))
    merged = view.merge_selections({})
    day = ScheduleService.build_day(archived, date(2026, 11, 5), merged, "仅备注")

    assert view.editable_people == ()
    assert day.assignments == []
    assert day.note == "仅备注"
