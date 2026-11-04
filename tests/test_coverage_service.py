"""按班次最低人数计算缺岗预警。"""

from __future__ import annotations

from datetime import date

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    SchedulePlan,
    Shift,
)
from src.services.config_service import ConfigService
from src.services.coverage_service import CoverageService
from tests.helpers import TEST_TIME


def test_missing_shift_is_reported_on_planned_day(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    report = CoverageService.build_report(base_settings, base_plan)

    # 11 月 2 日只排了早班和夜班，中班的最低人数 1 未满足。
    assert report.checked_dates == (date(2026, 11, 2),)
    assert len(report.shortages) == 1
    shortage = report.shortages[0]
    assert shortage.shift_name == "中班"
    assert (shortage.required, shortage.assigned, shortage.shortage) == (1, 0, 1)
    assert report.total_gap == 1


def test_min_staff_zero_disables_requirement(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    shifts = [
        Shift.model_validate({**shift.model_dump(), "min_staff": 0})
        if shift.id == "shift_middle"
        else shift
        for shift in base_settings.shifts
    ]
    settings = AppSettings.model_validate(
        {**base_settings.model_dump(), "shifts": [s.model_dump() for s in shifts]}
    )

    report = CoverageService.build_report(settings, base_plan)

    assert report.shortages == ()


def test_note_only_day_is_checked_but_empty_plan_is_not(
    base_settings: AppSettings,
) -> None:
    note_day = date(2026, 11, 8)
    plan = SchedulePlan(
        id="plan_coverage_note",
        name="备注日",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
        days=[DaySchedule(date=note_day, note="检修")],
    )

    report = CoverageService.build_report(base_settings, plan)
    empty_report = CoverageService.build_report(
        base_settings,
        SchedulePlan(
            id="plan_coverage_empty",
            name="空方案",
            created_at=TEST_TIME,
            updated_at=TEST_TIME,
        ),
    )

    assert report.checked_dates == (note_day,)
    assert len(report.shortages) == 3
    assert report.total_gap == 3
    assert empty_report.checked_dates == ()
    assert empty_report.shortages == ()


def test_minimum_count_can_be_greater_than_one(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    settings = ConfigService.update_shift(
        base_settings,
        "shift_early",
        name="早班",
        color="#F59E0B",
        load_weight=1.0,
        min_staff=2,
        is_early=True,
        is_night=False,
    )

    report = CoverageService.build_report(settings, base_plan)

    early = next(item for item in report.shortages if item.shift_id == "shift_early")
    assert early.required == 2
    assert early.assigned == 1
    assert early.shortage == 1


def test_inactive_shift_is_not_required(
    base_settings: AppSettings,
) -> None:
    settings = ConfigService.set_shift_active(base_settings, "shift_middle", False)
    plan = SchedulePlan(
        id="plan_coverage_archived_shift",
        name="停用班次",
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

    report = CoverageService.build_report(settings, plan)

    assert {item.shift_id for item in report.shortages} == {"shift_night"}
