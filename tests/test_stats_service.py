"""Tab 3 统计口径：启用人员、加权负荷、偏好与请假天数。"""

from __future__ import annotations

from datetime import date

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    SchedulePlan,
)
from src.services.config_service import ConfigService
from src.services.stats_service import StatsService
from tests.helpers import TEST_TIME


def test_stats_report_explicit_rest_days_without_load(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_stats_rest",
        name="休息统计",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 1),
                rest_person_ids=["person_a"],
            ),
            DaySchedule(
                date=date(2026, 11, 2),
                rest_person_ids=["person_a", "person_b"],
            ),
        ],
    )

    stats = StatsService.build(base_settings, plan)

    by_name = {row.name: row for row in stats.people}
    assert by_name["张三"].rest_days == 2
    assert by_name["李四"].rest_days == 1
    assert by_name["张三"].assignment_count == 0
    assert stats.total_weighted_load == 0.0


def test_team_stats_include_zero_load_people_and_exclude_archived(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.add_person(
        base_settings,
        name="王五",
        preferred_shift_ids=[],
    )
    settings = ConfigService.add_person(
        settings,
        name="赵六",
        preferred_shift_ids=[],
    )
    archived_person_id = next(
        person.id for person in settings.people if person.name == "赵六"
    )
    settings = ConfigService.set_person_active(
        settings, archived_person_id, False
    )
    settings = ConfigService.add_leave(
        settings,
        person_id="person_a",
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        reason="",
    )
    settings = ConfigService.add_leave(
        settings,
        person_id="person_a",
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3),
        reason="",
    )

    plan = SchedulePlan(
        id="plan_stats",
        name="统计",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(
                date=date(2026, 11, 1),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early")
                ],
            ),
            DaySchedule(
                date=date(2026, 11, 2),
                assignments=[
                    Assignment(person_id="person_a", shift_id="shift_early"),
                    Assignment(person_id="person_b", shift_id="shift_night"),
                    Assignment(
                        person_id=archived_person_id,
                        shift_id="shift_middle",
                    ),
                ],
            ),
            DaySchedule(date=date(2026, 11, 3), note="盘点"),
            DaySchedule(date=date(2026, 11, 4), note="收尾"),
        ],
    )

    stats = StatsService.build(settings, plan)

    assert stats.active_people_count == 3
    assert stats.total_assignments == 3
    assert stats.total_weighted_load == 3.6
    assert stats.average_weighted_load == 1.2
    assert stats.load_spread == 2.0
    assert stats.preference_total == 3
    assert stats.preference_satisfied == 3
    assert stats.preference_rate == 1.0

    by_name = {row.name: row for row in stats.people}
    assert set(by_name) == {"张三", "李四", "王五"}

    a = by_name["张三"]
    assert a.assignment_count == 2
    assert a.weighted_load == 2.0
    assert a.preference_satisfied == 2
    assert a.preference_total == 2
    assert a.leave_days == 3
    assert [(load.shift_name, load.assignment_count) for load in a.shift_loads] == [
        ("早班", 2)
    ]

    b = by_name["李四"]
    assert b.weighted_load == 1.6
    assert b.leave_days == 0

    c = by_name["王五"]
    assert c.weighted_load == 0
    assert c.assignment_count == 0
    assert c.leave_days == 0
    assert c.preference_rate is None


def test_empty_plan_returns_zero_stats_for_active_people(
    base_settings: AppSettings,
) -> None:
    plan = SchedulePlan(
        id="plan_stats_empty",
        name="空统计",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )

    stats = StatsService.build(base_settings, plan)

    assert stats.active_people_count == 2
    assert stats.people
    assert all(row.weighted_load == 0 for row in stats.people)
    assert stats.total_weighted_load == 0
    assert stats.load_spread == 0
    assert stats.preference_rate is None


def test_leave_days_are_clipped_to_plan_span(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 10, 25),
        end_date=date(2026, 11, 10),
        reason="长假",
    )
    plan = SchedulePlan(
        id="plan_stats_clip",
        name="裁切",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[
            DaySchedule(date=date(2026, 11, 1), note="开始"),
            DaySchedule(date=date(2026, 11, 3), note="结束"),
        ],
    )

    stats = StatsService.build(settings, plan)
    row = next(item for item in stats.people if item.person_id == "person_a")

    assert row.leave_days == 3
