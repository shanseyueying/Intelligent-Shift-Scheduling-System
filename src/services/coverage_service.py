"""按班次最低人数计算缺岗预警。

该结果属于运营预警，故意不计入健康分：最低人数的配置会因门店/业务变化，
不应把健康分变成“配置覆盖率”的分数。只检查方案中已有排班或备注的日期。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.models import AppSettings, SchedulePlan


@dataclass(frozen=True)
class ShiftShortage:
    date: date
    shift_id: str
    shift_name: str
    required: int
    assigned: int
    shortage: int


@dataclass(frozen=True)
class CoverageReport:
    checked_dates: tuple[date, ...]
    shortages: tuple[ShiftShortage, ...]

    @property
    def total_gap(self) -> int:
        return sum(item.shortage for item in self.shortages)

    @property
    def has_shortages(self) -> bool:
        return bool(self.shortages)


class CoverageService:
    @staticmethod
    def build_report(
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> CoverageReport:
        active_shifts = [shift for shift in settings.shifts if shift.is_active]
        checked_dates = tuple(sorted({day.date for day in plan.days}))
        shortages: list[ShiftShortage] = []

        for day in sorted(plan.days, key=lambda item: item.date):
            assigned_by_shift: dict[str, int] = {}
            for assignment in day.assignments:
                assigned_by_shift[assignment.shift_id] = (
                    assigned_by_shift.get(assignment.shift_id, 0) + 1
                )
            for shift in active_shifts:
                if shift.min_staff <= 0:
                    continue
                assigned = assigned_by_shift.get(shift.id, 0)
                gap = shift.min_staff - assigned
                if gap > 0:
                    shortages.append(
                        ShiftShortage(
                            date=day.date,
                            shift_id=shift.id,
                            shift_name=shift.name,
                            required=shift.min_staff,
                            assigned=assigned,
                            shortage=gap,
                        )
                    )

        return CoverageReport(
            checked_dates=checked_dates,
            shortages=tuple(shortages),
        )
