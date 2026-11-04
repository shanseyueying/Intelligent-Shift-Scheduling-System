"""高负荷人员的两两换班推荐。

AI 生成的初始逻辑：
最初枚举所有人员两两交换所有班次，并对每个候选做完整健康分评估。
人工发现的边缘 Case：
1) 交换后目标人员在同一天可能出现两个班次，需跳过对方日期已被目标占用的情况；
2) 同一日期内交换班次也应支持，且不能产生重复人员记录；
3) 请假、停用人员或停用班次不应进入推荐；
4) “高负荷”必须用班次权重，而不是单纯次数；
5) 候选数量可能随排班量平方增长，因此先用“目标潜在减负量”贪心排序并设评估上限；
6) 不能推荐健康分倒退的交换，至少要满足全部硬约束且健康分不低于当前；
7) 推荐可能被用户放置一段时间，UI 需要用方案/配置指纹防止旧候选被误应用。

最终方案：
仅交换双方已有的启用班次；按潜在目标负荷下降量预筛，最多评估 400 个候选；
每个候选完整检查硬约束与健康分；使用“负荷优先 + 健康分不倒退”护栏，返回 Top 3。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    HistoryAuditEntry,
    Person,
    SchedulePlan,
    Shift,
)
from src.models.common import utc_now
from src.services.constraint_service import ConstraintService
from src.services.leave_service import LeaveService
from src.services.plan_service import PlanService
from src.services.scoring_service import HealthResult, ScoringService


class SwapServiceError(ValueError):
    """可直接展示给用户的调班搜索错误。"""


@dataclass(frozen=True)
class _AssignmentSlot:
    person_id: str
    date: date
    shift_id: str
    shift_weight: float


@dataclass(frozen=True)
class SwapRecommendation:
    target_person_id: str
    partner_person_id: str
    target_date: date
    target_shift_id: str
    partner_date: date
    partner_shift_id: str
    candidate_plan: SchedulePlan
    target_load_before: float
    target_load_after: float
    partner_load_before: float
    partner_load_after: float
    health_before: int
    health_after: int

    @property
    def target_load_reduction(self) -> float:
        return round(self.target_load_before - self.target_load_after, 3)

    @property
    def health_delta(self) -> int:
        return self.health_after - self.health_before


class SwapService:
    MAX_EVALUATED_SWAPS = 400
    MAX_RESULTS = 3
    _EPSILON = 1e-9

    @classmethod
    def weighted_loads(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
    ) -> dict[str, float]:
        active_people = {person.id for person in settings.people if person.is_active}
        weights = {shift.id: shift.load_weight for shift in settings.shifts}
        loads = {person.id: 0.0 for person in settings.people if person.is_active}
        for day in plan.days:
            for assignment in day.assignments:
                if assignment.person_id not in active_people:
                    continue
                loads[assignment.person_id] = loads.get(
                    assignment.person_id, 0.0
                ) + weights.get(assignment.shift_id, 0.0)
        return loads

    @classmethod
    def recommend(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        target_person_id: str,
        *,
        now: datetime | None = None,
        limit: int = MAX_RESULTS,
    ) -> tuple[SwapRecommendation, ...]:
        people = {person.id: person for person in settings.people}
        shifts = {shift.id: shift for shift in settings.shifts}
        target = people.get(target_person_id)
        if target is None:
            raise SwapServiceError("目标人员不存在")
        if not target.is_active:
            raise SwapServiceError("目标人员已停用，不能生成调班推荐")

        current_health = ScoringService.evaluate(settings, plan)
        if current_health.score is None:
            return ()

        loads = cls.weighted_loads(settings, plan)
        target_slots = cls._slots(
            plan,
            people,
            shifts,
            only_person_id=target_person_id,
        )
        if not target_slots:
            return ()
        partner_slots = [
            slot
            for slot in cls._slots(plan, people, shifts, only_person_id=None)
            if slot.person_id != target_person_id
        ]
        if not partner_slots:
            return ()

        occupied = {
            (day.date, assignment.person_id)
            for day in plan.days
            for assignment in day.assignments
        }

        pairs: list[tuple[float, _AssignmentSlot, _AssignmentSlot]] = []
        for target_slot in target_slots:
            for partner_slot in partner_slots:
                potential_reduction = (
                    target_slot.shift_weight - partner_slot.shift_weight
                )
                if potential_reduction <= cls._EPSILON:
                    continue
                if target_slot.date != partner_slot.date:
                    if (partner_slot.date, target_slot.person_id) in occupied:
                        continue
                    if (target_slot.date, partner_slot.person_id) in occupied:
                        continue
                if LeaveService.is_on_leave(
                    settings, target_slot.person_id, partner_slot.date
                ):
                    continue
                if LeaveService.is_on_leave(
                    settings, partner_slot.person_id, target_slot.date
                ):
                    continue
                pairs.append((potential_reduction, target_slot, partner_slot))

        pairs.sort(
            key=lambda item: (
                -item[0],
                item[1].date,
                item[1].person_id,
                item[2].date,
                item[2].person_id,
            )
        )

        timestamp = now or utc_now()
        feasible: list[SwapRecommendation] = []
        for _, target_slot, partner_slot in pairs[: cls.MAX_EVALUATED_SWAPS]:
            candidate = cls._build_candidate_plan(
                plan,
                target_slot,
                partner_slot,
                timestamp,
            )
            if candidate is None:
                continue
            if ConstraintService.find_hard_violations(settings, candidate):
                continue
            candidate_health = ScoringService.evaluate(settings, candidate)
            if candidate_health.score is None:
                continue
            if candidate_health.score < current_health.score:
                continue

            target_load_before = round(loads.get(target_slot.person_id, 0.0), 3)
            target_load_after = round(
                target_load_before
                - target_slot.shift_weight
                + partner_slot.shift_weight,
                3,
            )
            partner_load_before = round(
                loads.get(partner_slot.person_id, 0.0), 3
            )
            partner_load_after = round(
                partner_load_before
                - partner_slot.shift_weight
                + target_slot.shift_weight,
                3,
            )

            feasible.append(
                SwapRecommendation(
                    target_person_id=target_slot.person_id,
                    partner_person_id=partner_slot.person_id,
                    target_date=target_slot.date,
                    target_shift_id=target_slot.shift_id,
                    partner_date=partner_slot.date,
                    partner_shift_id=partner_slot.shift_id,
                    candidate_plan=candidate,
                    target_load_before=target_load_before,
                    target_load_after=target_load_after,
                    partner_load_before=partner_load_before,
                    partner_load_after=partner_load_after,
                    health_before=current_health.score,
                    health_after=candidate_health.score,
                )
            )

        feasible.sort(
            key=lambda item: (
                -item.target_load_reduction,
                -item.health_after,
                item.partner_person_id,
                item.target_date,
                item.partner_date,
            )
        )
        top = feasible[:limit]
        return tuple(
            cls._with_audit(item, timestamp, current_health)
            for item in top
        )

    @staticmethod
    def _slots(
        plan: SchedulePlan,
        people: dict[str, Person],
        shifts: dict[str, Shift],
        only_person_id: str | None,
    ) -> list[_AssignmentSlot]:
        slots: list[_AssignmentSlot] = []
        for day in plan.days:
            for assignment in day.assignments:
                if (
                    only_person_id is not None
                    and assignment.person_id != only_person_id
                ):
                    continue
                person = people.get(assignment.person_id)
                shift = shifts.get(assignment.shift_id)
                if person is None or not person.is_active:
                    continue
                if shift is None or not shift.is_active:
                    continue
                slots.append(
                    _AssignmentSlot(
                        person_id=assignment.person_id,
                        date=day.date,
                        shift_id=assignment.shift_id,
                        shift_weight=shift.load_weight,
                    )
                )
        return slots

    @staticmethod
    def _build_candidate_plan(
        plan: SchedulePlan,
        target_slot: _AssignmentSlot,
        partner_slot: _AssignmentSlot,
        timestamp: datetime,
    ) -> SchedulePlan | None:
        modifications: dict[date, list[Assignment]] = {}

        if target_slot.date == partner_slot.date:
            day = plan.day_map[target_slot.date]
            remaining = [
                assignment
                for assignment in day.assignments
                if assignment.person_id
                not in {target_slot.person_id, partner_slot.person_id}
            ]
            remaining.extend(
                [
                    Assignment(
                        person_id=target_slot.person_id,
                        shift_id=partner_slot.shift_id,
                    ),
                    Assignment(
                        person_id=partner_slot.person_id,
                        shift_id=target_slot.shift_id,
                    ),
                ]
            )
            modifications[target_slot.date] = remaining
        else:
            target_day = plan.day_map[target_slot.date]
            target_remaining = [
                assignment
                for assignment in target_day.assignments
                if assignment.person_id != target_slot.person_id
            ]
            target_remaining.append(
                Assignment(
                    person_id=partner_slot.person_id,
                    shift_id=target_slot.shift_id,
                )
            )

            partner_day = plan.day_map[partner_slot.date]
            partner_remaining = [
                assignment
                for assignment in partner_day.assignments
                if assignment.person_id != partner_slot.person_id
            ]
            partner_remaining.append(
                Assignment(
                    person_id=target_slot.person_id,
                    shift_id=partner_slot.shift_id,
                )
            )
            modifications[target_slot.date] = target_remaining
            modifications[partner_slot.date] = partner_remaining

        try:
            days: list[DaySchedule] = []
            for day in plan.days:
                if day.date in modifications:
                    payload = day.model_dump()
                    payload["assignments"] = [
                        assignment.model_dump()
                        for assignment in modifications[day.date]
                    ]
                    days.append(DaySchedule.model_validate(payload))
                else:
                    days.append(day)
            return PlanService.with_days(plan, days, now=timestamp)
        except (ValueError, KeyError):
            return None

    @staticmethod
    def _with_audit(
        recommendation: SwapRecommendation,
        timestamp: datetime,
        current_health: HealthResult,
    ) -> SwapRecommendation:
        plan = recommendation.candidate_plan
        payload = plan.model_dump()
        audit = [entry.model_dump() for entry in plan.history_audit]
        audit.append(
            HistoryAuditEntry(
                timestamp=timestamp,
                action="swap_applied",
                detail={
                    "target_person_id": recommendation.target_person_id,
                    "partner_person_id": recommendation.partner_person_id,
                    "target_date": recommendation.target_date.isoformat(),
                    "target_shift_id": recommendation.target_shift_id,
                    "partner_date": recommendation.partner_date.isoformat(),
                    "partner_shift_id": recommendation.partner_shift_id,
                    "health_before": current_health.score,
                    "health_after": recommendation.health_after,
                    "target_load_before": recommendation.target_load_before,
                    "target_load_after": recommendation.target_load_after,
                },
            ).model_dump()
        )
        payload["history_audit"] = audit
        return SwapRecommendation(
            target_person_id=recommendation.target_person_id,
            partner_person_id=recommendation.partner_person_id,
            target_date=recommendation.target_date,
            target_shift_id=recommendation.target_shift_id,
            partner_date=recommendation.partner_date,
            partner_shift_id=recommendation.partner_shift_id,
            candidate_plan=SchedulePlan.model_validate(payload),
            target_load_before=recommendation.target_load_before,
            target_load_after=recommendation.target_load_after,
            partner_load_before=recommendation.partner_load_before,
            partner_load_after=recommendation.partner_load_after,
            health_before=recommendation.health_before,
            health_after=recommendation.health_after,
        )
