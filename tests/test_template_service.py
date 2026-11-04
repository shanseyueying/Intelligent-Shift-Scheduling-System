"""周期模板：计数、只补空白、冲突预检与参数校验。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.models import AppSettings, SchedulePlan
from src.services.config_service import ConfigService
from src.services.constraint_service import ConstraintService
from src.services.template_service import (
    TemplateRequest,
    TemplateService,
    TemplateServiceError,
)
from tests.helpers import TEST_TIME


def _request(
    *,
    start: date,
    end: date,
    people: tuple[str, ...],
    cycle: tuple[tuple[str | None, ...], ...],
) -> TemplateRequest:
    return TemplateRequest(
        start_date=start,
        end_date=end,
        person_ids=people,
        cycle=cycle,
    )


def test_preview_fills_cycle_and_counts_rest_cells(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 14),
        people=("person_a", "person_b"),
        cycle=(
            ("shift_early", "shift_middle"),
            ("shift_night", "shift_middle"),
            (None, "shift_early"),
        ),
    )

    preview = TemplateService.build_preview(
        base_settings,
        base_plan,
        request,
        now=TEST_TIME + timedelta(hours=1),
    )

    assert preview.added_count == 9
    assert preview.skipped_existing_count == 0
    assert preview.rest_cell_count == 1
    assert preview.new_hard_violations == ()
    assert preview.candidate_plan.id == base_plan.id
    assert len(preview.candidate_plan.days) == 6
    assert preview.candidate_plan.history_audit[-1].action == "template_applied"
    assert preview.candidate_plan.updated_at == TEST_TIME + timedelta(hours=1)
    assert len(base_plan.days) == 1
    assert preview.matches(base_plan, request)

    day_12 = next(
        day for day in preview.candidate_plan.days if day.date == date(2026, 11, 12)
    )
    assert [assignment.person_id for assignment in day_12.assignments] == ["person_b"]


def test_preview_only_fills_empty_slots_and_preserves_notes(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    request = _request(
        start=date(2026, 11, 2),
        end=date(2026, 11, 3),
        people=("person_a", "person_b"),
        cycle=(("shift_early", "shift_middle"),),
    )

    preview = TemplateService.build_preview(
        base_settings,
        base_plan,
        request,
        now=TEST_TIME + timedelta(hours=2),
    )

    assert preview.added_count == 2
    assert preview.skipped_existing_count == 2
    assert preview.new_hard_violations == ()

    day_2 = next(
        day for day in preview.candidate_plan.days if day.date == date(2026, 11, 2)
    )
    assert day_2.note == "夜班交接设备钥匙"
    assert {
        (assignment.person_id, assignment.shift_id)
        for assignment in day_2.assignments
    } == {("person_a", "shift_early"), ("person_b", "shift_night")}
    assert len(base_plan.days) == 1


def test_preview_flags_new_night_then_early_conflict(
    base_settings: AppSettings,
) -> None:
    empty_plan = SchedulePlan(
        id="plan_template_conflict",
        name="冲突模板",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )
    request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 11),
        people=("person_a",),
        cycle=(("shift_night",), ("shift_early",)),
    )

    preview = TemplateService.build_preview(base_settings, empty_plan, request)

    assert preview.added_count == 2
    assert len(preview.new_hard_violations) == 1
    assert (
        preview.new_hard_violations[0].code
        == ConstraintService.NIGHT_THEN_EARLY
    )
    assert preview.new_hard_violations[0].date == date(2026, 11, 11)


def test_template_skips_people_on_leave(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    settings = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 11, 10),
        end_date=date(2026, 11, 11),
        reason="休假",
    )
    request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 11),
        people=("person_a", "person_b"),
        cycle=(("shift_early", "shift_early"),),
    )

    preview = TemplateService.build_preview(settings, base_plan, request)

    assert preview.leave_skipped_count == 2
    assert preview.added_count == 2
    assert {item.person_id for item in preview.added} == {"person_b"}
    assert preview.new_hard_violations == ()


def test_template_rejects_archived_people_and_shifts(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    archived_person = ConfigService.set_person_active(
        base_settings, "person_a", False
    )
    person_request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 10),
        people=("person_a",),
        cycle=(("shift_early",),),
    )
    with pytest.raises(TemplateServiceError, match="已停用人员"):
        TemplateService.build_preview(archived_person, base_plan, person_request)

    archived_shift = ConfigService.set_shift_active(
        base_settings, "shift_early", False
    )
    shift_request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 10),
        people=("person_a",),
        cycle=(("shift_early",),),
    )
    with pytest.raises(TemplateServiceError, match="已停用班次"):
        TemplateService.build_preview(archived_shift, base_plan, shift_request)


def test_reapplying_same_template_adds_nothing(
    base_settings: AppSettings,
) -> None:
    request = _request(
        start=date(2026, 11, 10),
        end=date(2026, 11, 12),
        people=("person_a", "person_b"),
        cycle=(("shift_early", "shift_middle"), ("shift_middle", "shift_night")),
    )
    empty_plan = SchedulePlan(
        id="plan_template_idempotent",
        name="幂等模板",
        created_at=TEST_TIME,
        updated_at=TEST_TIME,
    )
    first = TemplateService.build_preview(base_settings, empty_plan, request)
    second = TemplateService.build_preview(
        base_settings,
        first.candidate_plan,
        request,
    )

    assert first.added_count == 6
    assert second.added_count == 0
    assert second.skipped_existing_count == 6
    assert second.new_hard_violations == ()


@pytest.mark.parametrize(
    ("template_request", "message"),
    [
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 9),
                people=("person_a",),
                cycle=(("shift_early",),),
            ),
            "结束日期不能早于",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 11),
                people=(),
                cycle=((),),
            ),
            "至少选择一名",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 11),
                people=("person_a", "person_a"),
                cycle=(("shift_early", "shift_early"),),
            ),
            "重复项",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 11),
                people=("person_missing",),
                cycle=(("shift_early",),),
            ),
            "不存在的人员",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 11),
                people=("person_a", "person_b"),
                cycle=(("shift_early",),),
            ),
            "班次数与参与人数不一致",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 11),
                people=("person_a",),
                cycle=(("shift_missing",),),
            ),
            "不存在的班次",
        ),
        (
            _request(
                start=date(2026, 1, 1),
                end=date(2026, 1, 1)
                + timedelta(days=TemplateService.MAX_RANGE_DAYS),
                people=("person_a",),
                cycle=(("shift_early",),),
            ),
            "最多支持",
        ),
        (
            _request(
                start=date(2026, 11, 10),
                end=date(2026, 11, 10),
                people=("person_a",),
                cycle=tuple(("shift_early",) for _ in range(15)),
            ),
            "周期最长",
        ),
    ],
)
def test_invalid_template_requests_raise(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
    template_request: TemplateRequest,
    message: str,
) -> None:
    with pytest.raises(TemplateServiceError, match=message):
        TemplateService.build_preview(base_settings, base_plan, template_request)
