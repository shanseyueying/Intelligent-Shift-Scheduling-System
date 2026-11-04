"""方案生命周期服务测试。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.models import SchedulePlan
from src.services.plan_service import PlanService, PlanServiceError
from tests.helpers import TEST_TIME


def test_duplicate_plan_is_independent_and_has_new_identity(
    base_plan: SchedulePlan,
) -> None:
    copied = PlanService.duplicate_plan(
        base_plan,
        "复制方案",
        now=TEST_TIME + timedelta(hours=1),
    )

    assert copied.id != base_plan.id
    assert copied.name == "复制方案"
    assert copied.days == base_plan.days
    assert copied.history_audit[0].action == "plan_copied"
    assert copied.history_audit[0].detail["source_plan_id"] == base_plan.id

    copied.days[0].note = "只改复制版本"
    assert base_plan.days[0].note == "夜班交接设备钥匙"


def test_rename_plan_updates_timestamp_and_revalidates_name(
    base_plan: SchedulePlan,
) -> None:
    renamed = PlanService.rename_plan(
        base_plan,
        "新名字",
        now=TEST_TIME + timedelta(days=1),
    )
    assert renamed.name == "新名字"
    assert renamed.updated_at == TEST_TIME + timedelta(days=1)
    assert base_plan.name == "基础方案"

    with pytest.raises(ValidationError):
        PlanService.rename_plan(base_plan, "   ")


def test_with_days_replaces_schedule_without_mutating_source(
    base_plan: SchedulePlan,
) -> None:
    updated = PlanService.with_days(
        base_plan,
        [],
        now=TEST_TIME + timedelta(hours=2),
    )
    assert updated.days == []
    assert len(base_plan.days) == 1
    assert updated.updated_at == TEST_TIME + timedelta(hours=2)


def test_plan_name_uniqueness_is_case_insensitive(
    base_plan: SchedulePlan,
) -> None:
    other = PlanService.create_plan("另一个方案", now=TEST_TIME)

    with pytest.raises(PlanServiceError, match="已存在"):
        PlanService.ensure_unique_name(
            "  基础方案  ",
            [base_plan, other],
        )

    PlanService.ensure_unique_name(
        "基础方案",
        [base_plan, other],
        exclude_plan_id=base_plan.id,
    )


def test_plan_name_rejects_empty_and_overlong(
    base_plan: SchedulePlan,
) -> None:
    with pytest.raises(PlanServiceError, match="不能为空"):
        PlanService.ensure_unique_name("   ", [base_plan])
    with pytest.raises(PlanServiceError, match="最多 50"):
        PlanService.ensure_unique_name("名" * 51, [base_plan])
