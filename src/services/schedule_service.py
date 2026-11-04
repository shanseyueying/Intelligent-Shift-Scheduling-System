"""单日排班编辑服务。

UI 只负责收集表单值；这里负责引用完整性、单人单日唯一性和方案替换。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    Person,
    SchedulePlan,
    Shift,
)
from src.services.leave_service import LeaveService
from src.services.plan_service import PlanService


class ScheduleServiceError(ValueError):
    """可直接展示给用户的排班编辑错误。"""


@dataclass(frozen=True)
class DayEditView:
    """编辑某天时的可用选项与需原样保留的历史安排。"""

    editable_people: tuple[Person, ...]
    active_shifts: tuple[Shift, ...]
    preserved_assignments: tuple[Assignment, ...]
    on_leave_people: tuple[Person, ...]
    initial_shift_by_person: dict[str, str | None]
    all_person_ids: tuple[str, ...]
    rest_person_ids: tuple[str, ...] = ()
    preserved_rest_person_ids: tuple[str, ...] = ()

    def merge_selections(
        self,
        selections: Mapping[str, str | None],
    ) -> dict[str, str | None]:
        """把可编辑人员的表单值合并上必须保留的历史安排。"""
        expected = {person.id for person in self.editable_people}
        if set(selections) != expected:
            raise ScheduleServiceError("可编辑人员与提交结果不一致，请重新打开弹窗")

        active_shift_ids = {shift.id for shift in self.active_shifts}
        for person_id, shift_id in selections.items():
            if shift_id is not None and shift_id not in active_shift_ids:
                raise ScheduleServiceError("只能为新排班选择启用中的班次")

        merged: dict[str, str | None] = {
            person_id: None for person_id in self.all_person_ids
        }
        for assignment in self.preserved_assignments:
            merged[assignment.person_id] = assignment.shift_id
        merged.update(selections)
        return merged


class ScheduleService:
    @staticmethod
    def build_edit_view(
        settings: AppSettings,
        plan: SchedulePlan,
        on_date: date,
    ) -> DayEditView:
        """拆分可编辑项与停用人员/班次形成的历史保留项。"""
        people = {person.id: person for person in settings.people}
        shifts = {shift.id: shift for shift in settings.shifts}
        current_day = plan.day_map.get(on_date)
        assignments = current_day.assignments if current_day else []
        rest_ids = set(current_day.rest_person_ids) if current_day else set()

        for assignment in assignments:
            if assignment.person_id not in people:
                raise ScheduleServiceError(
                    f"该日存在未知人员引用: {assignment.person_id}"
                )
            if assignment.shift_id not in shifts:
                raise ScheduleServiceError(
                    f"该日存在未知班次引用: {assignment.shift_id}"
                )
        for person_id in rest_ids:
            if person_id not in people:
                raise ScheduleServiceError(
                    f"该日存在未知休息人员引用: {person_id}"
                )

        on_leave_ids = LeaveService.people_on_leave(settings, on_date)
        preserved = tuple(
            assignment
            for assignment in assignments
            if not people[assignment.person_id].is_active
            or not shifts[assignment.shift_id].is_active
            or assignment.person_id in on_leave_ids
        )
        preserved_people = {assignment.person_id for assignment in preserved}
        editable_people = tuple(
            person
            for person in settings.people
            if person.is_active
            and person.id not in preserved_people
            and person.id not in on_leave_ids
        )
        editable_ids = {person.id for person in editable_people}
        current_shift = {
            assignment.person_id: assignment.shift_id for assignment in assignments
        }
        initial = {
            person.id: current_shift.get(person.id) for person in editable_people
        }
        active_shifts = tuple(shift for shift in settings.shifts if shift.is_active)
        on_leave_people = tuple(
            person
            for person in settings.people
            if person.is_active and person.id in on_leave_ids
        )

        return DayEditView(
            editable_people=editable_people,
            active_shifts=active_shifts,
            preserved_assignments=preserved,
            on_leave_people=on_leave_people,
            initial_shift_by_person=initial,
            all_person_ids=tuple(person.id for person in settings.people),
            rest_person_ids=tuple(
                person.id
                for person in settings.people
                if person.id in rest_ids and person.id in editable_ids
            ),
            preserved_rest_person_ids=tuple(
                person.id
                for person in settings.people
                if person.id in rest_ids and person.id not in editable_ids
            ),
        )

    @staticmethod
    def build_day(
        settings: AppSettings,
        on_date: date,
        selected_shift_ids: Mapping[str, str | None],
        note: str,
        *,
        rest_person_ids: Iterable[str] = (),
    ) -> DaySchedule:
        """根据“人员 -> 班次/None”和显式休息人员构造 DaySchedule。

        selected_shift_ids 必须覆盖配置中的全部人员。强制全覆盖是为了避免
        UI 漏行时把某人的已有排班悄悄删掉。None 表示该人当天未排班；
        只有 rest_person_ids 中的人员才会被记为“显式休息”。
        """
        expected_people = {person.id for person in settings.people}
        provided_people = set(selected_shift_ids)
        if provided_people != expected_people:
            missing = sorted(expected_people - provided_people)
            unknown = sorted(provided_people - expected_people)
            details: list[str] = []
            if missing:
                details.append(f"缺少人员 {missing}")
            if unknown:
                details.append(f"未知人员 {unknown}")
            raise ScheduleServiceError("当天编辑数据不完整：" + "；".join(details))

        known_shifts = {shift.id for shift in settings.shifts}
        assignments: list[Assignment] = []
        for person in settings.people:
            shift_id = selected_shift_ids[person.id]
            if shift_id is None:
                continue
            if shift_id not in known_shifts:
                raise ScheduleServiceError(
                    f"人员“{person.name}”选择了不存在的班次: {shift_id}"
                )
            assignments.append(
                Assignment(person_id=person.id, shift_id=shift_id)
            )

        rest_ids = list(dict.fromkeys(rest_person_ids))
        unknown_rest = set(rest_ids) - expected_people
        if unknown_rest:
            raise ScheduleServiceError(
                f"休息人员引用不存在: {sorted(unknown_rest)}"
            )
        assigned_ids = {assignment.person_id for assignment in assignments}
        overlap = assigned_ids & set(rest_ids)
        if overlap:
            raise ScheduleServiceError(
                f"同一人不能同时排班和休息: {sorted(overlap)}"
            )
        ordered_rest_ids = [
            person.id for person in settings.people if person.id in set(rest_ids)
        ]

        try:
            return DaySchedule(
                date=on_date,
                assignments=assignments,
                rest_person_ids=ordered_rest_ids,
                note=note,
            )
        except ValidationError as exc:
            raise ScheduleServiceError(f"当天排班校验失败：{exc}") from exc

    @staticmethod
    def upsert_day(
        plan: SchedulePlan,
        day: DaySchedule,
        *,
        now=None,
    ) -> SchedulePlan:
        """新增或替换某日；无班次、无休息且无备注时移除该日。"""
        retained = [existing for existing in plan.days if existing.date != day.date]
        if day.assignments or day.rest_person_ids or day.note:
            retained.append(day)
        retained.sort(key=lambda item: item.date)
        try:
            return PlanService.with_days(plan, retained, now=now)
        except ValidationError as exc:
            raise ScheduleServiceError(f"保存当天排班失败：{exc}") from exc
