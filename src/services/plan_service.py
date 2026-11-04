"""方案生命周期服务：新建、复制、重命名。

该层不直接读写文件，由调用方决定何时持久化，便于单元测试和未来复用。
"""

from __future__ import annotations

from datetime import datetime

from src.models import DaySchedule, HistoryAuditEntry, SchedulePlan
from src.models.common import utc_now


class PlanServiceError(ValueError):
    """可直接展示给用户的方案操作错误。"""


class PlanService:
    @staticmethod
    def ensure_unique_name(
        name: str,
        existing_plans: list[SchedulePlan],
        *,
        exclude_plan_id: str | None = None,
    ) -> None:
        normalized = name.strip()
        if not normalized:
            raise PlanServiceError("方案名称不能为空")
        if len(normalized) > 50:
            raise PlanServiceError("方案名称最多 50 个字符")
        for plan in existing_plans:
            if plan.id == exclude_plan_id:
                continue
            if plan.name.casefold() == normalized.casefold():
                raise PlanServiceError(f"方案名称“{normalized}”已存在")

    @staticmethod
    def create_plan(name: str, *, now: datetime | None = None) -> SchedulePlan:
        timestamp = now or utc_now()
        return SchedulePlan(
            name=name,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def duplicate_plan(
        source: SchedulePlan,
        new_name: str,
        *,
        now: datetime | None = None,
    ) -> SchedulePlan:
        """深拷贝全部日安排；新方案拥有独立 ID 与审计起点。"""
        timestamp = now or utc_now()
        copied_days = [day.model_copy(deep=True) for day in source.days]
        audit = HistoryAuditEntry(
            timestamp=timestamp,
            action="plan_copied",
            detail={"source_plan_id": source.id, "source_plan_name": source.name},
        )
        return SchedulePlan(
            name=new_name,
            created_at=timestamp,
            updated_at=timestamp,
            days=copied_days,
            history_audit=[audit],
        )

    @staticmethod
    def rename_plan(
        plan: SchedulePlan,
        new_name: str,
        *,
        now: datetime | None = None,
    ) -> SchedulePlan:
        """返回重新校验过的新对象，避免 model_copy 跳过字段校验。"""
        timestamp = now or utc_now()
        payload = plan.model_dump()
        payload.update(name=new_name, updated_at=timestamp)
        return SchedulePlan.model_validate(payload)

    @staticmethod
    def with_days(
        plan: SchedulePlan,
        days: list[DaySchedule],
        *,
        now: datetime | None = None,
    ) -> SchedulePlan:
        """替换方案日安排并刷新更新时间，供后续编辑/修复服务复用。"""
        timestamp = now or utc_now()
        payload = plan.model_dump()
        payload.update(days=[day.model_dump() for day in days], updated_at=timestamp)
        return SchedulePlan.model_validate(payload)
