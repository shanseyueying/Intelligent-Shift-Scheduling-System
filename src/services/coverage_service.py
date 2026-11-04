"""按覆盖组或单班次最低人数计算缺岗预警。

该结果属于运营预警，故意不计入健康分：最低人数的配置会因门店/业务变化，
不应把健康分变成“配置覆盖率”的分数。只检查方案中已有排班或备注的日期。

AI 生成的初始逻辑：
最初把每个启用班次都当成独立岗位，分别检查 shift.min_staff。这样“白班”
无法表达等同于早班+中班的组合规则，排了白班+夜班仍会误报早班/中班缺岗。

人工发现的边缘 Case：
1) 同一天不同班次可能互相替代，不能只按班次名称逐项计数；
2) 一个班次不应同时属于两个启用中的覆盖组，否则同一人会被重复计数；
3) 停用组不能继续生效，否则历史配置会悄悄改变缺岗结果；
4) 未加入任何组的班次仍需保留原来的单班次最低人数行为；
5) 组内班次全部停用时，该组不应凭空制造一个永远无法满足的缺口。

最终修正方案：
新增 CoverageGroup（组名、最低人数、班次集合）。启用组的成员统一按组
计数；未入组或只在停用组里的班次继续按 shift.min_staff 单独检查。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.models import AppSettings, SchedulePlan


@dataclass(frozen=True)
class ShiftShortage:
    date: date
    rule_id: str
    rule_name: str
    required: int
    assigned: int
    shortage: int
    member_shift_names: tuple[str, ...] = ()
    is_group: bool = False

    @property
    def shift_id(self) -> str:
        """兼容既有调用方；组规则返回覆盖组 ID。"""
        return self.rule_id

    @property
    def shift_name(self) -> str:
        """兼容既有调用方；组规则返回覆盖组名称。"""
        return self.rule_name

    @property
    def display_name(self) -> str:
        if self.is_group and self.member_shift_names:
            return f"{self.rule_name}（{'、'.join(self.member_shift_names)}）"
        return self.rule_name


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
        active_by_id = {shift.id: shift for shift in active_shifts}
        active_groups = [
            group for group in settings.coverage_groups if group.is_active
        ]
        grouped_shift_ids = {
            shift_id
            for group in active_groups
            for shift_id in group.shift_ids
            if shift_id in active_by_id
        }
        checked_dates = tuple(sorted({day.date for day in plan.days}))
        shortages: list[ShiftShortage] = []

        for day in sorted(plan.days, key=lambda item: item.date):
            assigned_by_shift: dict[str, int] = {}
            for assignment in day.assignments:
                assigned_by_shift[assignment.shift_id] = (
                    assigned_by_shift.get(assignment.shift_id, 0) + 1
                )

            for group in active_groups:
                member_shifts = [
                    active_by_id[shift_id]
                    for shift_id in group.shift_ids
                    if shift_id in active_by_id
                ]
                if not member_shifts or group.min_staff <= 0:
                    continue
                assigned = sum(
                    assigned_by_shift.get(shift.id, 0)
                    for shift in member_shifts
                )
                gap = group.min_staff - assigned
                if gap > 0:
                    shortages.append(
                        ShiftShortage(
                            date=day.date,
                            rule_id=group.id,
                            rule_name=group.name,
                            required=group.min_staff,
                            assigned=assigned,
                            shortage=gap,
                            member_shift_names=tuple(
                                shift.name for shift in member_shifts
                            ),
                            is_group=True,
                        )
                    )

            for shift in active_shifts:
                if shift.id in grouped_shift_ids or shift.min_staff <= 0:
                    continue
                assigned = assigned_by_shift.get(shift.id, 0)
                gap = shift.min_staff - assigned
                if gap > 0:
                    shortages.append(
                        ShiftShortage(
                            date=day.date,
                            rule_id=shift.id,
                            rule_name=shift.name,
                            required=shift.min_staff,
                            assigned=assigned,
                            shortage=gap,
                            member_shift_names=(shift.name,),
                        )
                    )

        return CoverageReport(
            checked_dates=checked_dates,
            shortages=tuple(shortages),
        )
