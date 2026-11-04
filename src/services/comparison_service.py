"""两个排班方案的差异计算。

对比方向固定为“当前方案相对参考方案”：
- added：当前方案有、参考方案无；
- removed：参考方案有、当前方案无；
- changed：两边同一天同一人但班次/休息状态不同。
显式休息作为内部状态参与比较，避免“休息 vs 未安排”被漏掉。
只返回变化项，未变化数量单独统计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.models import SchedulePlan


@dataclass(frozen=True)
class AssignmentDiff:
    change_type: str
    date: date
    person_id: str
    current_shift_id: str | None
    reference_shift_id: str | None


@dataclass(frozen=True)
class NoteDiff:
    date: date
    current_note: str
    reference_note: str


@dataclass(frozen=True)
class PlanComparison:
    current_plan_id: str
    reference_plan_id: str
    assignment_diffs: tuple[AssignmentDiff, ...]
    note_diffs: tuple[NoteDiff, ...]
    unchanged_assignment_count: int

    @property
    def added_count(self) -> int:
        return sum(
            1
            for item in self.assignment_diffs
            if item.change_type == ComparisonService.ADDED
        )

    @property
    def removed_count(self) -> int:
        return sum(
            1
            for item in self.assignment_diffs
            if item.change_type == ComparisonService.REMOVED
        )

    @property
    def changed_count(self) -> int:
        return sum(
            1
            for item in self.assignment_diffs
            if item.change_type == ComparisonService.CHANGED
        )

    @property
    def note_change_count(self) -> int:
        return len(self.note_diffs)

    @property
    def has_changes(self) -> bool:
        return bool(self.assignment_diffs or self.note_diffs)


class ComparisonService:
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    REST = "__REST__"

    @classmethod
    def compare(
        cls,
        current: SchedulePlan,
        reference: SchedulePlan,
    ) -> PlanComparison:
        current_assignments = cls._status_map(current)
        reference_assignments = cls._status_map(reference)

        diffs: list[AssignmentDiff] = []
        unchanged = 0
        all_keys = sorted(set(current_assignments) | set(reference_assignments))
        for key in all_keys:
            on_date, person_id = key
            current_shift = current_assignments.get(key)
            reference_shift = reference_assignments.get(key)
            if current_shift == reference_shift:
                unchanged += 1
                continue
            if reference_shift is None:
                change_type = cls.ADDED
            elif current_shift is None:
                change_type = cls.REMOVED
            else:
                change_type = cls.CHANGED
            diffs.append(
                AssignmentDiff(
                    change_type=change_type,
                    date=on_date,
                    person_id=person_id,
                    current_shift_id=current_shift,
                    reference_shift_id=reference_shift,
                )
            )

        current_notes = {
            day.date: day.note for day in current.days if day.note
        }
        reference_notes = {
            day.date: day.note for day in reference.days if day.note
        }
        note_diffs = tuple(
            NoteDiff(
                date=on_date,
                current_note=current_notes.get(on_date, ""),
                reference_note=reference_notes.get(on_date, ""),
            )
            for on_date in sorted(set(current_notes) | set(reference_notes))
            if current_notes.get(on_date, "") != reference_notes.get(on_date, "")
        )

        return PlanComparison(
            current_plan_id=current.id,
            reference_plan_id=reference.id,
            assignment_diffs=tuple(diffs),
            note_diffs=note_diffs,
            unchanged_assignment_count=unchanged,
        )

    @classmethod
    def _status_map(cls, plan: SchedulePlan) -> dict[tuple[date, str], str]:
        result: dict[tuple[date, str], str] = {}
        for day in plan.days:
            for assignment in day.assignments:
                result[(day.date, assignment.person_id)] = assignment.shift_id
            for person_id in day.rest_person_ids:
                result[(day.date, person_id)] = cls.REST
        return result
