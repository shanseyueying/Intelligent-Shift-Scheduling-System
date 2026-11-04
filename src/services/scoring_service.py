"""健康评分：硬约束否决 + 三项软约束扣分。

加权负荷只统计启用中且有排班的人员；没有排班数据时不显示 100 分，
而是返回“暂无排班”，避免空方案被误认为完美。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from src.models import AppSettings, SchedulePlan
from src.services.constraint_service import ConstraintService, HardConstraintViolation

# AI 生成的初始逻辑：
# 最初把任何连续同班次都扣分、用人员总负荷的平均值衡量均衡，并直接
# 用“满足偏好次数”扣分。
#
# 人工发现的边缘 Case：
# 1) 连续两天同班次是正常排班，从第 3 天起才计为“连续多天”；
# 2) 只按平均负荷会忽略极端值，改用有排班人员之间的加权负荷极差；
# 3) 偏好满足应按未满足比例计分，不能因为排班总量多就无限放大扣分；
# 4) 有硬约束时即使软分很高也不能显示健康，否则会给出危险信号；
# 5) 空方案没有排班，不应得到 100 分，应显示“暂无排班”。
#
# 最终修正方案：
# 显式扫描同班次连续区间；负荷按极差 ×5 扣分并封顶；偏好按未满足比例
# ×20 扣分；硬约束直接令总分 0/不合格；无排班数据时 score=None。


@dataclass(frozen=True)
class SoftConstraintBreakdown:
    continuity_penalty: int
    continuity_extra_days: int
    load_penalty: int
    load_spread: float
    preference_penalty: int
    preference_total: int
    preference_satisfied: int

    @property
    def total_penalty(self) -> int:
        return (
            self.continuity_penalty
            + self.load_penalty
            + self.preference_penalty
        )

    @property
    def preference_rate(self) -> float | None:
        if self.preference_total == 0:
            return None
        return self.preference_satisfied / self.preference_total


@dataclass(frozen=True)
class HealthResult:
    score: int | None
    status: str
    hard_violations: tuple[HardConstraintViolation, ...]
    soft: SoftConstraintBreakdown
    assignment_count: int

    @property
    def has_data(self) -> bool:
        return self.assignment_count > 0


class ScoringService:
    CONTINUITY_CAP = 20
    LOAD_CAP = 25
    PREFERENCE_CAP = 20
    HEALTHY_THRESHOLD = 85
    ATTENTION_THRESHOLD = 70

    STATUS_NO_DATA = "暂无排班"
    STATUS_FAILED = "不合格"
    STATUS_HEALTHY = "健康"
    STATUS_ATTENTION = "需要关注"
    STATUS_OPTIMIZE = "需要优化"

    @classmethod
    def evaluate(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> HealthResult:
        hard_violations = ConstraintService.find_hard_violations(settings, plan)
        assignment_count = sum(len(day.assignments) for day in plan.days)

        continuity_extra_days = cls._continuity_extra_days(plan)
        continuity_penalty = min(
            cls.CONTINUITY_CAP,
            2 * continuity_extra_days,
        )

        load_spread = cls._weighted_load_spread(settings, plan)
        load_penalty = min(
            cls.LOAD_CAP,
            int(load_spread * 5 + 0.5),
        )

        pref_total, pref_satisfied = cls._preference_counts(settings, plan)
        if pref_total:
            unmet_rate = 1 - (pref_satisfied / pref_total)
            preference_penalty = min(
                cls.PREFERENCE_CAP,
                int(unmet_rate * cls.PREFERENCE_CAP + 0.5),
            )
        else:
            preference_penalty = 0

        breakdown = SoftConstraintBreakdown(
            continuity_penalty=continuity_penalty,
            continuity_extra_days=continuity_extra_days,
            load_penalty=load_penalty,
            load_spread=round(load_spread, 3),
            preference_penalty=preference_penalty,
            preference_total=pref_total,
            preference_satisfied=pref_satisfied,
        )

        if assignment_count == 0:
            return HealthResult(
                score=None,
                status=cls.STATUS_NO_DATA,
                hard_violations=hard_violations,
                soft=breakdown,
                assignment_count=0,
            )

        if hard_violations:
            return HealthResult(
                score=0,
                status=cls.STATUS_FAILED,
                hard_violations=hard_violations,
                soft=breakdown,
                assignment_count=assignment_count,
            )

        score = max(0, 100 - breakdown.total_penalty)
        if score >= cls.HEALTHY_THRESHOLD:
            status = cls.STATUS_HEALTHY
        elif score >= cls.ATTENTION_THRESHOLD:
            status = cls.STATUS_ATTENTION
        else:
            status = cls.STATUS_OPTIMIZE

        return HealthResult(
            score=score,
            status=status,
            hard_violations=hard_violations,
            soft=breakdown,
            assignment_count=assignment_count,
        )

    @staticmethod
    def _continuity_extra_days(plan: SchedulePlan) -> int:
        by_person: dict[str, list[tuple[date, str]]] = {}
        for day in plan.days:
            for assignment in day.assignments:
                by_person.setdefault(assignment.person_id, []).append(
                    (day.date, assignment.shift_id)
                )

        extra_days = 0
        for entries in by_person.values():
            ordered = sorted(entries, key=lambda item: item[0])
            run_length = 0
            previous_date: date | None = None
            previous_shift: str | None = None
            for current_date, current_shift in ordered:
                continues = (
                    previous_date is not None
                    and current_date - previous_date == timedelta(days=1)
                    and current_shift == previous_shift
                )
                if continues:
                    run_length += 1
                else:
                    if run_length >= 3:
                        extra_days += run_length - 2
                    run_length = 1
                previous_date = current_date
                previous_shift = current_shift
            if run_length >= 3:
                extra_days += run_length - 2
        return extra_days

    @staticmethod
    def _weighted_load_spread(
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> float:
        active_people = {person.id for person in settings.people if person.is_active}
        shift_weights = {shift.id: shift.load_weight for shift in settings.shifts}

        loads: dict[str, float] = {}
        for day in plan.days:
            for assignment in day.assignments:
                if assignment.person_id not in active_people:
                    continue
                loads[assignment.person_id] = loads.get(
                    assignment.person_id, 0.0
                ) + shift_weights.get(assignment.shift_id, 0.0)

        if len(loads) < 2:
            return 0.0
        return max(loads.values()) - min(loads.values())

    @staticmethod
    def _preference_counts(
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> tuple[int, int]:
        preference_by_person = {
            person.id: set(person.preferred_shift_ids)
            for person in settings.people
            if person.is_active and person.preferred_shift_ids
        }

        total = 0
        satisfied = 0
        for day in plan.days:
            for assignment in day.assignments:
                preferences = preference_by_person.get(assignment.person_id)
                if not preferences:
                    continue
                total += 1
                if assignment.shift_id in preferences:
                    satisfied += 1
        return total, satisfied
