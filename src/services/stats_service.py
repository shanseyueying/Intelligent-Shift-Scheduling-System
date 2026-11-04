"""Tab 3 的加权负荷、偏好和请假可用性统计。

统计口径（已确认）：包含全部启用人员，缺少排班的人以 0 负荷显示。
请假天数按方案最早/最晚日期区间计算，重叠请假区间只计一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.models import AppSettings, SchedulePlan


@dataclass(frozen=True)
class ShiftLoad:
    shift_id: str
    shift_name: str
    assignment_count: int
    weighted_load: float


@dataclass(frozen=True)
class PersonStats:
    person_id: str
    name: str
    assignment_count: int
    weighted_load: float
    preference_total: int
    preference_satisfied: int
    leave_days: int
    rest_days: int
    shift_loads: tuple[ShiftLoad, ...]

    @property
    def preference_rate(self) -> float | None:
        if self.preference_total == 0:
            return None
        return self.preference_satisfied / self.preference_total


@dataclass(frozen=True)
class TeamStats:
    people: tuple[PersonStats, ...]
    active_people_count: int
    total_assignments: int
    total_weighted_load: float
    average_weighted_load: float
    load_spread: float
    preference_total: int
    preference_satisfied: int

    @property
    def preference_rate(self) -> float | None:
        if self.preference_total == 0:
            return None
        return self.preference_satisfied / self.preference_total


class StatsService:
    @classmethod
    def build(cls, settings: AppSettings, plan: SchedulePlan) -> TeamStats:
        active_people = [person for person in settings.people if person.is_active]
        shift_by_id = {shift.id: shift for shift in settings.shifts}
        shift_order = {
            shift.id: index for index, shift in enumerate(settings.shifts)
        }
        assignments_by_person: dict[str, list[str]] = {}
        rest_days_by_person: dict[str, int] = {}

        for day in plan.days:
            for assignment in day.assignments:
                assignments_by_person.setdefault(
                    assignment.person_id, []
                ).append(assignment.shift_id)
            for person_id in day.rest_person_ids:
                rest_days_by_person[person_id] = (
                    rest_days_by_person.get(person_id, 0) + 1
                )

        plan_start, plan_end = cls._plan_span(plan)
        person_rows: list[PersonStats] = []

        for person in active_people:
            entries = assignments_by_person.get(person.id, [])
            shift_counts: dict[str, int] = {}
            weighted_by_shift: dict[str, float] = {}
            weight_total = 0.0
            preferred = set(person.preferred_shift_ids)
            preference_total = 0
            preference_satisfied = 0

            for shift_id in entries:
                shift = shift_by_id.get(shift_id)
                shift_counts[shift_id] = shift_counts.get(shift_id, 0) + 1
                weight = shift.load_weight if shift is not None else 0.0
                weighted_by_shift[shift_id] = (
                    weighted_by_shift.get(shift_id, 0.0) + weight
                )
                weight_total += weight
                if preferred:
                    preference_total += 1
                    if shift_id in preferred:
                        preference_satisfied += 1

            shift_loads = tuple(
                ShiftLoad(
                    shift_id=shift_id,
                    shift_name=(
                        shift_by_id[shift_id].name
                        if shift_id in shift_by_id
                        else f"未知班次({shift_id})"
                    ),
                    assignment_count=shift_counts[shift_id],
                    weighted_load=round(weighted_by_shift[shift_id], 3),
                )
                for shift_id in sorted(
                    shift_counts,
                    key=lambda item: (shift_order.get(item, 10**6), item),
                )
            )

            person_rows.append(
                PersonStats(
                    person_id=person.id,
                    name=person.name,
                    assignment_count=len(entries),
                    weighted_load=round(weight_total, 3),
                    preference_total=preference_total,
                    preference_satisfied=preference_satisfied,
                    leave_days=cls._leave_days(
                        settings,
                        person.id,
                        plan_start,
                        plan_end,
                    ),
                    rest_days=rest_days_by_person.get(person.id, 0),
                    shift_loads=shift_loads,
                )
            )

        person_rows.sort(key=lambda item: (-item.weighted_load, item.name))
        loads = [row.weighted_load for row in person_rows]
        total_load = round(sum(loads), 3)
        average_load = (
            round(total_load / len(person_rows), 3) if person_rows else 0.0
        )
        spread = round(max(loads) - min(loads), 3) if loads else 0.0
        preference_total = sum(row.preference_total for row in person_rows)
        preference_satisfied = sum(
            row.preference_satisfied for row in person_rows
        )

        return TeamStats(
            people=tuple(person_rows),
            active_people_count=len(person_rows),
            total_assignments=sum(row.assignment_count for row in person_rows),
            total_weighted_load=total_load,
            average_weighted_load=average_load,
            load_spread=spread,
            preference_total=preference_total,
            preference_satisfied=preference_satisfied,
        )

    @staticmethod
    def _plan_span(plan: SchedulePlan) -> tuple[date | None, date | None]:
        if not plan.days:
            return None, None
        dates = [day.date for day in plan.days]
        return min(dates), max(dates)

    @staticmethod
    def _leave_days(
        settings: AppSettings,
        person_id: str,
        start: date | None,
        end: date | None,
    ) -> int:
        if start is None or end is None:
            return 0
        clipped: list[tuple[date, date]] = []
        for leave in settings.leaves:
            if leave.person_id != person_id:
                continue
            interval_start = max(leave.start_date, start)
            interval_end = min(leave.end_date, end)
            if interval_start <= interval_end:
                clipped.append((interval_start, interval_end))
        if not clipped:
            return 0

        clipped.sort()
        total = 0
        current_start, current_end = clipped[0]
        for next_start, next_end in clipped[1:]:
            if next_start <= current_end + timedelta(days=1):
                current_end = max(current_end, next_end)
            else:
                total += (current_end - current_start).days + 1
                current_start, current_end = next_start, next_end
        total += (current_end - current_start).days + 1
        return total
