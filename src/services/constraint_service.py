"""硬约束校验。

当前实现第一条跨日硬约束：夜班后“次日”禁止早班。
本模块不依赖 Streamlit，后续健康打分与智能修复会复用同一结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.models import AppSettings, SchedulePlan
from src.services.leave_service import LeaveService

# AI 生成的初始逻辑：
# 最初按班次名称中的“夜/早”关键词判断，并直接比较同一天数据列表中的相邻项。
#
# 人工发现的边缘 Case：
# 1) 自定义名称可能不含关键词（例如“凌晨班”），关键词匹配会漏判；
# 2) 同一人员的排班在 JSON 中不必按日期有序，直接比较相邻记录会误判；
# 3) 隔一天或更久的两次排班不属于“次日”，不能算违规；
# 4) 不同人员在相邻两天上夜班/早班不应互相影响。
#
# 最终修正方案：
# 用 Shift.is_night / is_early 显式标记语义；按人员分组并按日期排序；
# 只检查日期恰好相差 1 天的相邻记录。缺失班次引用时不猜测，交给数据完整性检查。


@dataclass(frozen=True)
class HardConstraintViolation:
    code: str
    person_id: str
    date: date
    message: str
    related_date: date | None = None

    @property
    def key(self) -> tuple[str, str, date, date | None]:
        return (self.code, self.person_id, self.date, self.related_date)


class ConstraintService:
    NIGHT_THEN_EARLY = "night_then_early"
    LEAVE_CONFLICT = "leave_conflict"

    @classmethod
    def find_hard_violations(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> tuple[HardConstraintViolation, ...]:
        people = {person.id: person for person in settings.people}
        shifts = {shift.id: shift for shift in settings.shifts}

        by_person: dict[str, list[tuple[date, str]]] = {}
        for day in plan.days:
            for assignment in day.assignments:
                by_person.setdefault(assignment.person_id, []).append(
                    (day.date, assignment.shift_id)
                )

        violations: list[HardConstraintViolation] = []
        for person_id, entries in by_person.items():
            ordered = sorted(entries, key=lambda item: item[0])
            person_name = people[person_id].name if person_id in people else person_id
            for previous, current in zip(ordered, ordered[1:]):
                previous_date, previous_shift_id = previous
                current_date, current_shift_id = current
                if (current_date - previous_date).days != 1:
                    continue
                previous_shift = shifts.get(previous_shift_id)
                current_shift = shifts.get(current_shift_id)
                if previous_shift is None or current_shift is None:
                    continue
                if previous_shift.is_night and current_shift.is_early:
                    violations.append(
                        HardConstraintViolation(
                            code=cls.NIGHT_THEN_EARLY,
                            person_id=person_id,
                            date=current_date,
                            related_date=previous_date,
                            message=(
                                f"{person_name} 在 {previous_date.isoformat()} "
                                f"上夜班“{previous_shift.name}”后，"
                                f"次日又排了早班“{current_shift.name}”"
                            ),
                        )
                    )

        for conflict in LeaveService.find_conflicts(settings, plan):
            person_name = (
                people[conflict.person_id].name
                if conflict.person_id in people
                else conflict.person_id
            )
            shift_name = (
                shifts[conflict.shift_id].name
                if conflict.shift_id in shifts
                else conflict.shift_id
            )
            violations.append(
                HardConstraintViolation(
                    code=cls.LEAVE_CONFLICT,
                    person_id=conflict.person_id,
                    date=conflict.date,
                    message=(
                        f"{person_name} 在请假期间（{conflict.date.isoformat()}）"
                        f"仍被安排班次“{shift_name}”"
                    ),
                )
            )

        violations.sort(key=lambda item: (item.date, item.person_id, item.code))
        return tuple(violations)

    @staticmethod
    def new_violations(
        before: tuple[HardConstraintViolation, ...],
        after: tuple[HardConstraintViolation, ...],
    ) -> tuple[HardConstraintViolation, ...]:
        before_keys = {violation.key for violation in before}
        return tuple(
            violation for violation in after if violation.key not in before_keys
        )
