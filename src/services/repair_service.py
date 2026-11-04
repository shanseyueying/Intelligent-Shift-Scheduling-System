"""针对单条硬约束的局部修复。

AI 生成的初始逻辑：
最初只尝试把违规人员的早班直接删掉，或把所有人员两两交换后取健康分最高者。
人工发现的边缘 Case：
1) 删除早班会制造覆盖缺口，不应作为首选；
2) 只交换“违规早班”有时无解，交换前一天的夜班也可能解除约束；
3) 交换/替补不能引入任何新的硬约束，但方案中其他既有冲突可以暂时保留；
4) 同一日期交换或跨日期交换都可能产生重复排班，需要预检占用；
5) 替补人员必须启用、当天未排班且未请假，接手早班后不能产生新的夜班→早班；
6) 只修一条违规时，不能把总违规数“换汤不换药”，必须消除选中违规；
7) 候选数必须有限，避免大方案下一次性尝试所有组合。

最终修正方案：
先尝试涉及违规早班/前夜班的已有班次交换；只有完全没有可行交换时，才寻找
当天闲置的启用人员接手早班。每个候选都检查“选中违规消失 + 不新增硬约束”，
再按剩余硬约束数和软约束扣分选择最佳，返回单个预览供用户确认。
"""

from __future__ import annotations

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
from src.services.scoring_service import HealthResult, ScoringService


@dataclass(frozen=True)
class RepairChange:
    action: str
    date: date
    person_id: str
    shift_id: str


@dataclass(frozen=True)
class RepairRecommendation:
    operation: str
    candidate_plan: SchedulePlan
    changes: tuple[RepairChange, ...]
    hard_before: int
    hard_after: int
    health_before: int | None
    health_after: int | None

    @property
    def removed_changes(self) -> tuple[RepairChange, ...]:
        return tuple(change for change in self.changes if change.action == "removed")

    @property
    def added_changes(self) -> tuple[RepairChange, ...]:
        return tuple(change for change in self.changes if change.action == "added")


@dataclass(frozen=True)
class RepairSearchResult:
    recommendation: RepairRecommendation | None
    reason: str = ""


@dataclass(frozen=True)
class _Slot:
    date: date
    person_id: str
    shift_id: str


@dataclass(frozen=True)
class _Candidate:
    operation: str
    candidate_plan: SchedulePlan
    changes: tuple[RepairChange, ...]
    hard_after: int
    health: HealthResult


class RepairService:
    SWAP = "swap"
    SUBSTITUTE = "substitute"
    MAX_SWAP_CANDIDATES = 500

    @classmethod
    def propose(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        violation: HardConstraintViolation,
        *,
        now: datetime | None = None,
    ) -> RepairSearchResult:
        if violation.code != ConstraintService.NIGHT_THEN_EARLY:
            return RepairSearchResult(
                recommendation=None,
                reason="当前只能对“夜班后次日早班”违规执行单项修复。",
            )

        current_violations = ConstraintService.find_hard_violations(settings, plan)
        current_keys = {item.key for item in current_violations}
        if violation.key not in current_keys:
            return RepairSearchResult(
                recommendation=None,
                reason="该违规已不存在，可能刚刚被其他操作修复。",
            )

        if violation.related_date is None:
            return RepairSearchResult(
                recommendation=None,
                reason="缺少夜班日期，无法计算修复。",
            )

        people = {person.id: person for person in settings.people}
        shifts = {shift.id: shift for shift in settings.shifts}
        person = people.get(violation.person_id)
        if person is None or not person.is_active:
            return RepairSearchResult(
                recommendation=None,
                reason="违规人员不存在或已停用。",
            )

        early_date = violation.date
        night_date = violation.related_date
        early_slot = cls._find_slot(plan, person.id, early_date)
        night_slot = cls._find_slot(plan, person.id, night_date)
        if early_slot is None or night_slot is None:
            return RepairSearchResult(
                recommendation=None,
                reason="未找到违规对应的夜班/早班记录。",
            )
        early_shift = shifts.get(early_slot.shift_id)
        night_shift = shifts.get(night_slot.shift_id)
        if (
            early_shift is None
            or night_shift is None
            or not early_shift.is_early
            or not night_shift.is_night
        ):
            return RepairSearchResult(
                recommendation=None,
                reason="班次早/夜属性已变化，请刷新后重新评估违规。",
            )
        if not early_shift.is_active or not night_shift.is_active:
            return RepairSearchResult(
                recommendation=None,
                reason="该违规涉及已停用班次，请先恢复相关班次后再尝试修复。",
            )

        current_health = ScoringService.evaluate(settings, plan)
        allowed_keys = current_keys - {violation.key}
        occupied = {
            (day.date, assignment.person_id)
            for day in plan.days
            for assignment in day.assignments
        }
        timestamp = now or utc_now()

        swap_candidates = cls._search_swaps(
            settings,
            plan,
            person.id,
            early_slot,
            night_slot,
            occupied,
            allowed_keys,
            timestamp,
        )
        candidates = swap_candidates
        if not candidates:
            candidates = cls._search_substitutes(
                settings,
                plan,
                person.id,
                early_slot,
                allowed_keys,
                timestamp,
            )

        if not candidates:
            return RepairSearchResult(
                recommendation=None,
                reason=(
                    "没有找到同时满足“消除该违规且不新增硬约束”的换班或替补方案。"
                    "可以把问题先保留在方案中，或手工调整后再试。"
                ),
            )

        candidates.sort(
            key=lambda item: (
                item.hard_after,
                item.health.soft.total_penalty,
                item.operation,
                tuple((change.date, change.person_id) for change in item.changes),
            )
        )
        best = candidates[0]
        recommendation = RepairRecommendation(
            operation=best.operation,
            candidate_plan=best.candidate_plan,
            changes=best.changes,
            hard_before=len(current_violations),
            hard_after=best.hard_after,
            health_before=current_health.score,
            health_after=best.health.score,
        )
        return RepairSearchResult(
            recommendation=cls._with_audit(recommendation, timestamp),
            reason="",
        )

    @classmethod
    def _search_swaps(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        target_person_id: str,
        early_slot: _Slot,
        night_slot: _Slot,
        occupied: set[tuple[date, str]],
        allowed_keys: set[tuple],
        timestamp: datetime,
    ) -> list[_Candidate]:
        partner_slots: list[_Slot] = []
        people = {person.id: person for person in settings.people}
        shifts = {shift.id: shift for shift in settings.shifts}
        for day in plan.days:
            for assignment in day.assignments:
                if assignment.person_id == target_person_id:
                    continue
                person = people.get(assignment.person_id)
                shift = shifts.get(assignment.shift_id)
                if person is None or not person.is_active:
                    continue
                if shift is None or not shift.is_active:
                    continue
                partner_slots.append(
                    _Slot(
                        date=day.date,
                        person_id=assignment.person_id,
                        shift_id=assignment.shift_id,
                    )
                )

        pairs: list[tuple[_Slot, _Slot]] = []
        for fix_slot in (early_slot, night_slot):
            for partner_slot in partner_slots:
                if fix_slot.date != partner_slot.date:
                    if (partner_slot.date, target_person_id) in occupied:
                        continue
                    if (fix_slot.date, partner_slot.person_id) in occupied:
                        continue
                if partner_slot.date == fix_slot.date and (
                    partner_slot.shift_id == fix_slot.shift_id
                ):
                    continue
                pairs.append((fix_slot, partner_slot))

        candidates: list[_Candidate] = []
        seen: set[tuple] = set()
        for fix_slot, partner_slot in pairs[: cls.MAX_SWAP_CANDIDATES]:
            removals = {
                (fix_slot.date, fix_slot.person_id),
                (partner_slot.date, partner_slot.person_id),
            }
            additions = [
                (partner_slot.date, fix_slot.person_id, partner_slot.shift_id),
                (fix_slot.date, partner_slot.person_id, fix_slot.shift_id),
            ]
            changes = tuple(
                sorted(
                    [
                        RepairChange(
                            "removed",
                            fix_slot.date,
                            fix_slot.person_id,
                            fix_slot.shift_id,
                        ),
                        RepairChange(
                            "removed",
                            partner_slot.date,
                            partner_slot.person_id,
                            partner_slot.shift_id,
                        ),
                        RepairChange(
                            "added",
                            partner_slot.date,
                            fix_slot.person_id,
                            partner_slot.shift_id,
                        ),
                        RepairChange(
                            "added",
                            fix_slot.date,
                            partner_slot.person_id,
                            fix_slot.shift_id,
                        ),
                    ],
                    key=lambda item: (item.date, item.person_id, item.action),
                )
            )
            candidate = cls._candidate_from_changes(
                settings,
                plan,
                removals,
                additions,
                allowed_keys,
                timestamp,
                operation=cls.SWAP,
                changes=changes,
                seen=seen,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    @classmethod
    def _search_substitutes(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        target_person_id: str,
        early_slot: _Slot,
        allowed_keys: set[tuple],
        timestamp: datetime,
    ) -> list[_Candidate]:
        assigned_on_date = {
            assignment.person_id
            for day in plan.days
            if day.date == early_slot.date
            for assignment in day.assignments
        }
        resting_on_date = {
            person_id
            for day in plan.days
            if day.date == early_slot.date
            for person_id in day.rest_person_ids
        }
        on_leave = LeaveService.people_on_leave(settings, early_slot.date)
        candidates: list[_Candidate] = []
        seen: set[tuple] = set()
        for person in settings.people:
            if not person.is_active:
                continue
            if person.id == target_person_id:
                continue
            if (
                person.id in assigned_on_date
                or person.id in resting_on_date
                or person.id in on_leave
            ):
                continue
            removals = {(early_slot.date, target_person_id)}
            additions = [(early_slot.date, person.id, early_slot.shift_id)]
            changes = (
                RepairChange(
                    "removed",
                    early_slot.date,
                    target_person_id,
                    early_slot.shift_id,
                ),
                RepairChange(
                    "added",
                    early_slot.date,
                    person.id,
                    early_slot.shift_id,
                ),
            )
            candidate = cls._candidate_from_changes(
                settings,
                plan,
                removals,
                additions,
                allowed_keys,
                timestamp,
                operation=cls.SUBSTITUTE,
                changes=changes,
                seen=seen,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    @classmethod
    def _candidate_from_changes(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        removals: set[tuple[date, str]],
        additions: list[tuple[date, str, str]],
        allowed_keys: set[tuple],
        timestamp: datetime,
        *,
        operation: str,
        changes: tuple[RepairChange, ...],
        seen: set[tuple],
    ) -> _Candidate | None:
        candidate_plan = cls._apply_changes(
            plan,
            removals,
            additions,
            timestamp,
        )
        if candidate_plan is None:
            return None

        candidate_violations = ConstraintService.find_hard_violations(
            settings, candidate_plan
        )
        candidate_keys = {item.key for item in candidate_violations}
        if candidate_keys - allowed_keys:
            return None

        fingerprint = tuple(
            sorted(
                (day.date, assignment.person_id, assignment.shift_id)
                for day in candidate_plan.days
                for assignment in day.assignments
            )
        )
        if fingerprint in seen:
            return None
        seen.add(fingerprint)

        candidate_health = ScoringService.evaluate(settings, candidate_plan)
        return _Candidate(
            operation=operation,
            candidate_plan=candidate_plan,
            changes=changes,
            hard_after=len(candidate_keys),
            health=candidate_health,
        )

    @staticmethod
    def _apply_changes(
        plan: SchedulePlan,
        removals: set[tuple[date, str]],
        additions: list[tuple[date, str, str]],
        timestamp: datetime,
    ) -> SchedulePlan | None:
        modifications: dict[date, list[Assignment]] = {}
        touched_dates = {date for date, _ in removals} | {
            date for date, _, _ in additions
        }
        for touched_date in touched_dates:
            day = plan.day_map.get(touched_date)
            if day is None:
                return None
            assignments = [
                assignment
                for assignment in day.assignments
                if (day.date, assignment.person_id) not in removals
            ]
            assignments.extend(
                Assignment(person_id=person_id, shift_id=shift_id)
                for on_date, person_id, shift_id in additions
                if on_date == touched_date
            )
            modifications[touched_date] = assignments

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
    def _find_slot(
        plan: SchedulePlan,
        person_id: str,
        on_date: date,
    ) -> _Slot | None:
        day = plan.day_map.get(on_date)
        if day is None:
            return None
        for assignment in day.assignments:
            if assignment.person_id == person_id:
                return _Slot(
                    date=on_date,
                    person_id=person_id,
                    shift_id=assignment.shift_id,
                )
        return None

    @staticmethod
    def _with_audit(
        recommendation: RepairRecommendation,
        timestamp: datetime,
    ) -> RepairRecommendation:
        plan = recommendation.candidate_plan
        payload = plan.model_dump()
        audit = [entry.model_dump() for entry in plan.history_audit]
        audit.append(
            HistoryAuditEntry(
                timestamp=timestamp,
                action="hard_violation_repaired",
                detail={
                    "operation": recommendation.operation,
                    "hard_before": recommendation.hard_before,
                    "hard_after": recommendation.hard_after,
                    "changes": [
                        {
                            "action": change.action,
                            "date": change.date.isoformat(),
                            "person_id": change.person_id,
                            "shift_id": change.shift_id,
                        }
                        for change in recommendation.changes
                    ],
                },
            ).model_dump()
        )
        payload["history_audit"] = audit
        return RepairRecommendation(
            operation=recommendation.operation,
            candidate_plan=SchedulePlan.model_validate(payload),
            changes=recommendation.changes,
            hard_before=recommendation.hard_before,
            hard_after=recommendation.hard_after,
            health_before=recommendation.health_before,
            health_after=recommendation.health_after,
        )
