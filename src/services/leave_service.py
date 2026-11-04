"""请假可用性、冲突检测与当前方案清理。

新排班/模板会跳过请假人员；已经存在的冲突不会静默删除，而是标为硬约束，
由用户在预览确认后执行“移除当前方案冲突班次”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.models import (
    AppSettings,
    DaySchedule,
    HistoryAuditEntry,
    SchedulePlan,
)
from src.models.common import utc_now
from src.services.plan_service import PlanService


@dataclass(frozen=True)
class LeaveAssignmentConflict:
    leave_id: str
    person_id: str
    date: date
    shift_id: str


@dataclass(frozen=True)
class LeaveCleanupResult:
    plan: SchedulePlan
    removed: tuple[LeaveAssignmentConflict, ...]


class LeaveService:
    @staticmethod
    def people_on_leave(settings: AppSettings, on_date: date) -> set[str]:
        return {
            leave.person_id
            for leave in settings.leaves
            if leave.start_date <= on_date <= leave.end_date
        }

    @staticmethod
    def is_on_leave(
        settings: AppSettings,
        person_id: str,
        on_date: date,
    ) -> bool:
        return any(
            leave.person_id == person_id
            and leave.start_date <= on_date <= leave.end_date
            for leave in settings.leaves
        )

    @classmethod
    def find_conflicts(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> tuple[LeaveAssignmentConflict, ...]:
        """同一人重叠的请假只报告一次，避免重复计数。"""
        conflicts: dict[tuple[date, str], LeaveAssignmentConflict] = {}
        for day in plan.days:
            for assignment in day.assignments:
                key = (day.date, assignment.person_id)
                if key in conflicts:
                    continue
                for leave in settings.leaves:
                    if (
                        leave.person_id == assignment.person_id
                        and leave.start_date <= day.date <= leave.end_date
                    ):
                        conflicts[key] = LeaveAssignmentConflict(
                            leave_id=leave.id,
                            person_id=assignment.person_id,
                            date=day.date,
                            shift_id=assignment.shift_id,
                        )
                        break

        return tuple(
            sorted(
                conflicts.values(),
                key=lambda item: (item.date, item.person_id),
            )
        )

    @classmethod
    def remove_conflicts(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        *,
        now: datetime | None = None,
    ) -> LeaveCleanupResult:
        """移除当前方案中与请假冲突的班次，保留备注与审计记录。"""
        conflicts = cls.find_conflicts(settings, plan)
        if not conflicts:
            return LeaveCleanupResult(plan=plan, removed=())

        timestamp = now or utc_now()
        conflict_pairs = {(item.date, item.person_id) for item in conflicts}
        days: list[DaySchedule] = []
        for day in plan.days:
            kept = [
                assignment
                for assignment in day.assignments
                if (day.date, assignment.person_id) not in conflict_pairs
            ]
            if not kept and not day.note:
                continue
            if len(kept) != len(day.assignments):
                payload = day.model_dump()
                payload["assignments"] = [
                    assignment.model_dump() for assignment in kept
                ]
                days.append(DaySchedule.model_validate(payload))
            else:
                days.append(day)

        candidate = PlanService.with_days(plan, days, now=timestamp)
        payload = candidate.model_dump()
        audit = [entry.model_dump() for entry in candidate.history_audit]
        audit.append(
            HistoryAuditEntry(
                timestamp=timestamp,
                action="leave_conflicts_removed",
                detail={
                    "removed_count": len(conflicts),
                    "removed": [
                        {
                            "date": item.date.isoformat(),
                            "person_id": item.person_id,
                            "shift_id": item.shift_id,
                        }
                        for item in conflicts
                    ],
                },
            ).model_dump()
        )
        payload["history_audit"] = audit
        candidate = SchedulePlan.model_validate(payload)
        return LeaveCleanupResult(plan=candidate, removed=conflicts)
