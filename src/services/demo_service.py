"""首次启动时生成一份可编辑的示例工作区。"""

from __future__ import annotations

from src.models import AppSettings, Person, SchedulePlan, Shift
from src.services.plan_service import PlanService


def build_demo_workspace() -> tuple[AppSettings, SchedulePlan]:
    """仅在数据目录完全为空时调用，不覆盖任何已有文件。"""
    early = Shift(
        name="早班",
        color="#F59E0B",
        load_weight=1.0,
        is_early=True,
    )
    middle = Shift(
        name="中班",
        color="#3B82F6",
        load_weight=1.1,
    )
    night = Shift(
        name="夜班",
        color="#7C3AED",
        load_weight=1.6,
        is_night=True,
    )

    people = [
        Person(name="张明", preferred_shift_ids=[early.id]),
        Person(name="李婷", preferred_shift_ids=[middle.id]),
        Person(name="王浩", preferred_shift_ids=[night.id]),
        Person(name="赵敏", preferred_shift_ids=[]),
    ]
    plan = PlanService.create_plan("示例排班")
    settings = AppSettings(
        people=people,
        shifts=[early, middle, night],
        leaves=[],
        active_plan_id=plan.id,
    )
    return settings, plan
