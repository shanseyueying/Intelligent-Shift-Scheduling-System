"""周期模板铺排：生成候选、差异预览与安全检查。

交互约定（已确认）：
- 模板是一个 1–14 天的循环矩阵，行=周期日、列=参与人员；
- 应用到日期区间时只填空白人员，不覆盖已有安排；
- 返回候选方案供 UI 预览，确认后才持久化。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    HistoryAuditEntry,
    SchedulePlan,
)
from src.models.common import utc_now
from src.services.constraint_service import ConstraintService, HardConstraintViolation
from src.services.leave_service import LeaveService
from src.services.plan_service import PlanService
from src.utils.dates import iter_days

# AI 生成的初始逻辑：
# 首版按“当天一旦有排班就跳过整天”避免覆盖，并直接修改原 SchedulePlan。
#
# 人工发现的边缘 Case：
# 1) 当天已有 A 的班次，不代表 B 的空白格不能补，整日跳过会造成不必要的缺岗；
# 2) 直接修改原对象时，如果落盘失败，内存与磁盘会被悄悄分开；
# 3) 模板跨周期边界（最后一天夜班接下一次循环第一天早班）需要预检；
# 4) 交接备注属于整天信息，不能因补一个人班次而被清空；
# 5) 预览后用户可能修改参数，必须防止旧预览被错误应用。
#
# 最终修正方案：
# 以“日期 + 人员”为补位粒度，只填空白并保留原备注；生成全新候选方案；
# 保存前由 UI 校验预览指纹；同时对比套用前后的硬约束集合，只把新增冲突
# 展示给用户，并要求显式确认后才允许覆盖硬约束建议。


class TemplateServiceError(ValueError):
    """模板参数错误，可直接展示给用户。"""


@dataclass(frozen=True)
class TemplateRequest:
    start_date: date
    end_date: date
    person_ids: tuple[str, ...]
    cycle: tuple[tuple[str | None, ...], ...]

    @property
    def range_days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def signature(self) -> str:
        payload = {
            "start": self.start_date.isoformat(),
            "end": self.end_date.isoformat(),
            "people": list(self.person_ids),
            "cycle": [list(row) for row in self.cycle],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TemplateAddition:
    date: date
    person_id: str
    shift_id: str


@dataclass(frozen=True)
class TemplatePreview:
    request: TemplateRequest
    source_plan_id: str
    source_plan_updated_at: datetime
    candidate_plan: SchedulePlan
    added: tuple[TemplateAddition, ...]
    skipped_existing_count: int
    leave_skipped_count: int
    rest_cell_count: int
    existing_violation_count: int
    new_hard_violations: tuple[HardConstraintViolation, ...]

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def has_changes(self) -> bool:
        return bool(self.added)

    def matches(self, plan: SchedulePlan, request: TemplateRequest) -> bool:
        return (
            self.source_plan_id == plan.id
            and self.source_plan_updated_at == plan.updated_at
            and self.request == request
        )


class TemplateService:
    MAX_RANGE_DAYS = 92
    MAX_CYCLE_DAYS = 14

    @classmethod
    def validate_request(
        cls,
        settings: AppSettings,
        request: TemplateRequest,
    ) -> None:
        if request.end_date < request.start_date:
            raise TemplateServiceError("结束日期不能早于开始日期")
        if request.range_days > cls.MAX_RANGE_DAYS:
            raise TemplateServiceError(
                f"单次铺排最多支持 {cls.MAX_RANGE_DAYS} 天，"
                f"当前选择 {request.range_days} 天"
            )
        if not request.person_ids:
            raise TemplateServiceError("请至少选择一名参与人员")
        if len(set(request.person_ids)) != len(request.person_ids):
            raise TemplateServiceError("参与人员中存在重复项")

        people = {person.id: person for person in settings.people}
        unknown_people = set(request.person_ids) - set(people)
        if unknown_people:
            raise TemplateServiceError(
                f"模板引用了不存在的人员: {sorted(unknown_people)}"
            )
        inactive_people = [
            people[person_id].name
            for person_id in request.person_ids
            if not people[person_id].is_active
        ]
        if inactive_people:
            raise TemplateServiceError(
                "模板包含已停用人员: " + "、".join(inactive_people)
            )

        if not request.cycle:
            raise TemplateServiceError("周期模板至少需要一天")
        if len(request.cycle) > cls.MAX_CYCLE_DAYS:
            raise TemplateServiceError(
                f"周期最长 {cls.MAX_CYCLE_DAYS} 天，当前为 {len(request.cycle)} 天"
            )

        shifts = {shift.id: shift for shift in settings.shifts}
        for row_index, row in enumerate(request.cycle, start=1):
            if len(row) != len(request.person_ids):
                raise TemplateServiceError(
                    f"周期第 {row_index} 天的班次数与参与人数不一致"
                )
            for shift_id in row:
                if shift_id is None:
                    continue
                if shift_id not in shifts:
                    raise TemplateServiceError(
                        f"周期第 {row_index} 天引用了不存在的班次: {shift_id}"
                    )
                if not shifts[shift_id].is_active:
                    raise TemplateServiceError(
                        f"周期第 {row_index} 天使用了已停用班次"
                        f"“{shifts[shift_id].name}”"
                    )

    @classmethod
    def build_preview(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        request: TemplateRequest,
        *,
        now: datetime | None = None,
    ) -> TemplatePreview:
        """生成全新方案对象；原 plan 与任何 DaySchedule 都不会被修改。"""
        cls.validate_request(settings, request)
        timestamp = now or utc_now()

        existing_violations = ConstraintService.find_hard_violations(settings, plan)
        days_by_date = {day.date: day for day in plan.days}
        added: list[TemplateAddition] = []
        skipped_existing = 0
        leave_skipped = 0
        rest_cells = 0

        for offset, on_date in enumerate(
            iter_days(request.start_date, request.end_date)
        ):
            cycle_row = request.cycle[offset % len(request.cycle)]
            current_day = days_by_date.get(on_date)
            occupied_people = (
                {assignment.person_id for assignment in current_day.assignments}
                if current_day
                else set()
            )
            on_leave = LeaveService.people_on_leave(settings, on_date)
            new_assignments: list[Assignment] = []
            for person_id, shift_id in zip(request.person_ids, cycle_row):
                if shift_id is None:
                    rest_cells += 1
                    continue
                if person_id in on_leave:
                    leave_skipped += 1
                    continue
                if person_id in occupied_people:
                    skipped_existing += 1
                    continue
                new_assignments.append(
                    Assignment(person_id=person_id, shift_id=shift_id)
                )
                added.append(
                    TemplateAddition(
                        date=on_date,
                        person_id=person_id,
                        shift_id=shift_id,
                    )
                )

            if not new_assignments:
                continue

            payload = (
                current_day.model_dump()
                if current_day
                else {"date": on_date.isoformat(), "assignments": [], "note": ""}
            )
            payload["assignments"] = [
                *payload["assignments"],
                *(assignment.model_dump() for assignment in new_assignments),
            ]
            days_by_date[on_date] = DaySchedule.model_validate(payload)

        all_days = [days_by_date[day] for day in sorted(days_by_date)]
        candidate_plan = PlanService.with_days(plan, all_days, now=timestamp)

        if added:
            candidate_payload = candidate_plan.model_dump()
            audit_entries = [
                entry.model_dump() for entry in candidate_plan.history_audit
            ]
            audit_entries.append(
                HistoryAuditEntry(
                    timestamp=timestamp,
                    action="template_applied",
                    detail={
                        "start_date": request.start_date.isoformat(),
                        "end_date": request.end_date.isoformat(),
                        "person_ids": list(request.person_ids),
                        "cycle_days": len(request.cycle),
                        "added_count": len(added),
                    },
                ).model_dump()
            )
            candidate_payload["history_audit"] = audit_entries
            candidate_plan = SchedulePlan.model_validate(candidate_payload)

        candidate_violations = ConstraintService.find_hard_violations(
            settings, candidate_plan
        )
        new_violations = ConstraintService.new_violations(
            existing_violations, candidate_violations
        )

        return TemplatePreview(
            request=request,
            source_plan_id=plan.id,
            source_plan_updated_at=plan.updated_at,
            candidate_plan=candidate_plan,
            added=tuple(added),
            skipped_existing_count=skipped_existing,
            leave_skipped_count=leave_skipped,
            rest_cell_count=rest_cells,
            existing_violation_count=len(existing_violations),
            new_hard_violations=new_violations,
        )
