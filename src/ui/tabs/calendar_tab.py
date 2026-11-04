"""Tab 1：可编辑月历与周期模板铺排。

流程：
1. FullCalendar 返回 dateClick / eventClick → 统一打开当天的全员编辑弹窗；
2. 弹窗保存经过 ScheduleService 校验并原子落盘；
3. 周期模板先生成预览（只补空白），确认后才应用。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from streamlit_calendar import calendar

from src.models import AppSettings, Person, SchedulePlan, Shift
from src.services.constraint_service import ConstraintService, HardConstraintViolation
from src.services.coverage_service import CoverageService
from src.services.schedule_service import ScheduleService, ScheduleServiceError
from src.services.template_service import (
    TemplatePreview,
    TemplateRequest,
    TemplateService,
    TemplateServiceError,
)
from src.ui.state import persist_active_plan, set_flash
from src.utils.dates import local_date_from_iso, month_end, month_start
from src.utils.storage import JsonStorage, StorageError

_REST_OPTION = "__REST__"
_SELECTED_DATE = "calendar.selected_date"
_CALENDAR_INITIAL_DATE = "calendar.initial_date"
_CALENDAR_VERSION = "calendar.version"

_TEMPLATE_START = "template.start_date"
_TEMPLATE_END = "template.end_date"
_TEMPLATE_PEOPLE = "template.person_ids"
_TEMPLATE_CYCLE_DAYS = "template.cycle_days"
_TEMPLATE_CELLS = "template.cells"
_TEMPLATE_PREVIEW = "template.preview"

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _text_color(hex_color: str) -> str:
    color = hex_color.lstrip("#")
    red, green, blue = (int(color[index : index + 2], 16) for index in (0, 2, 4))
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#0F172A" if luminance > 0.66 else "#FFFFFF"


def _shift_label(shift_id: str, shifts: dict[str, Shift]) -> str:
    shift = shifts.get(shift_id)
    return shift.name if shift else f"未知班次({shift_id})"


def _format_shift_option(value: str, shifts: dict[str, Shift]) -> str:
    if value == _REST_OPTION:
        return "休息"
    shift = shifts.get(value)
    return shift.name if shift else "未知班次"


def _ensure_calendar_state() -> None:
    st.session_state.setdefault(_CALENDAR_INITIAL_DATE, date.today())
    st.session_state.setdefault(_CALENDAR_VERSION, 0)
    st.session_state.setdefault(_SELECTED_DATE, None)


def _close_day_editor() -> None:
    """清理弹窗关联状态并重挂日历，防止旧点击值反复触发弹窗。"""
    if st.session_state.get(_SELECTED_DATE) is None:
        return
    st.session_state[_SELECTED_DATE] = None
    st.session_state[_CALENDAR_VERSION] = (
        int(st.session_state.get(_CALENDAR_VERSION, 0)) + 1
    )


def _clicked_date(component_value: Any) -> date | None:
    if not isinstance(component_value, dict):
        return None
    callback = component_value.get("callback")
    raw: str | None = None
    if callback == "dateClick":
        raw = component_value.get("dateClick", {}).get("date")
    elif callback == "eventClick":
        raw = component_value.get("eventClick", {}).get("event", {}).get("start")
    if not raw:
        return None
    try:
        return local_date_from_iso(str(raw))
    except (TypeError, ValueError):
        return None


def _render_constraint_banner(
    violations: tuple[HardConstraintViolation, ...],
) -> None:
    if not violations:
        return
    st.warning(f"当前方案有 {len(violations)} 个硬约束冲突。")
    with st.expander("查看硬约束冲突明细", expanded=False):
        for violation in violations:
            st.markdown(f"- {violation.message}")


@st.dialog(
    "编辑当天排班",
    width="large",
    on_dismiss=_close_day_editor,
)
def _day_editor_dialog(
    settings: AppSettings,
    plan: SchedulePlan,
    on_date: date,
    storage: JsonStorage,
) -> None:
    st.markdown(
        f"#### {on_date.isoformat()} · {_WEEKDAYS[on_date.weekday()]}"
    )
    st.caption(
        "每位人员最多一个班次；选择“休息”会清除该人当天安排。"
        "请假、已停用人员/班次的历史安排会自动保留。"
    )

    people = {person.id: person for person in settings.people}
    shifts = {shift.id: shift for shift in settings.shifts}
    current_day = plan.day_map.get(on_date)

    try:
        edit_view = ScheduleService.build_edit_view(settings, plan, on_date)
    except ScheduleServiceError as exc:
        st.error(f"这一天无法安全编辑：{exc}")
        st.caption("请先修复对应 JSON 文件或恢复缺失的配置。")
        if st.button("关闭", width="stretch"):
            _close_day_editor()
            st.rerun()
        return

    if edit_view.on_leave_people:
        names = "、".join(person.name for person in edit_view.on_leave_people)
        st.info(f"请假锁定：{names}。新安排不会提供给请假人员。")

    if edit_view.preserved_assignments:
        st.warning(
            "以下安排涉及请假、停用或历史数据，本次编辑会原样保留。"
            "请假冲突请在 Tab 2 中确认清理；停用项需先恢复配置后才能修改。"
        )
        preserved_rows = [
            {
                "人员": getattr(
                    people.get(assignment.person_id),
                    "name",
                    assignment.person_id,
                ),
                "班次": _shift_label(assignment.shift_id, shifts),
            }
            for assignment in edit_view.preserved_assignments
        ]
        st.dataframe(
            pd.DataFrame(preserved_rows),
            width="stretch",
            hide_index=True,
        )

    active_shifts = {shift.id: shift for shift in edit_view.active_shifts}
    editable_people = edit_view.editable_people
    version = int(st.session_state.get(_CALENDAR_VERSION, 0))
    editor_key = f"day_editor::{plan.id}::{on_date.isoformat()}::{version}"
    edited = None

    with st.form(f"day_form::{editor_key}", border=False):
        if editable_people:
            table = pd.DataFrame(
                {
                    "人员": [person.name for person in editable_people],
                    "班次": [
                        edit_view.initial_shift_by_person.get(
                            person.id, _REST_OPTION
                        )
                        or _REST_OPTION
                        for person in editable_people
                    ],
                },
                index=[person.id for person in editable_people],
            )
            edited = st.data_editor(
                table,
                width="stretch",
                hide_index=True,
                num_rows="fixed",
                height=min(420, 48 + 36 * len(editable_people)),
                disabled=["人员"],
                column_config={
                    "班次": st.column_config.SelectboxColumn(
                        "班次",
                        required=True,
                        options=[_REST_OPTION, *active_shifts.keys()],
                        format_func=lambda value: _format_shift_option(
                            value, active_shifts
                        ),
                    )
                },
                key=editor_key,
            )
        else:
            st.info("当前没有可编辑的启用人员；仍可填写或修改交接备注。")

        note = st.text_area(
            "交接备注（可选）",
            value=current_day.note if current_day else "",
            max_chars=1000,
            height=100,
            key=f"day_note::{editor_key}",
        )
        submitted = st.form_submit_button(
            "保存排班",
            type="primary",
            width="stretch",
        )

    if st.button("取消并关闭", width="stretch"):
        _close_day_editor()
        st.rerun()

    if not submitted:
        return

    selected: dict[str, str | None] = {}
    if editable_people:
        if edited is None or set(edited.index) != {
            person.id for person in editable_people
        }:
            st.error("排班表格结构异常，请关闭弹窗后重试。")
            return
        for person_id in edited.index:
            value = edited.at[person_id, "班次"]
            if value == _REST_OPTION:
                selected[str(person_id)] = None
            elif value in active_shifts:
                selected[str(person_id)] = str(value)
            else:
                st.error("存在无法识别的班次选项，请重新选择。")
                return

    try:
        merged = edit_view.merge_selections(selected)
        day = ScheduleService.build_day(settings, on_date, merged, note)
        updated = ScheduleService.upsert_day(plan, day)
        if updated.days == plan.days:
            set_flash("没有检测到排班变化。", "info")
            _close_day_editor()
            st.rerun()

        previous_violations = ConstraintService.find_hard_violations(settings, plan)
        current_violations = ConstraintService.find_hard_violations(settings, updated)
        new_violations = ConstraintService.new_violations(
            previous_violations, current_violations
        )
        persist_active_plan(storage, updated)
        st.session_state.pop(_TEMPLATE_PREVIEW, None)
    except (ScheduleServiceError, ValidationError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    if new_violations:
        set_flash(
            f"已保存当天排班，但新增 {len(new_violations)} 个硬约束冲突，"
            "请查看日历顶部提示。",
            "warning",
        )
    else:
        set_flash(f"已保存 {on_date.isoformat()} 的排班。", "success")
    _close_day_editor()
    st.rerun()


def _render_calendar(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
    events: list[dict[str, object]],
) -> None:
    _ensure_calendar_state()
    version = int(st.session_state.get(_CALENDAR_VERSION, 0))
    initial_date = st.session_state.get(_CALENDAR_INITIAL_DATE, date.today())

    component_value = calendar(
        events=events,
        callbacks=["dateClick", "eventClick"],
        options={
            "initialView": "dayGridMonth",
            "initialDate": initial_date.isoformat(),
            "locale": "zh-cn",
            "height": 660,
            "editable": False,
            "selectable": False,
            "dayMaxEvents": 3,
            "displayEventTime": False,
            "headerToolbar": {
                "left": "prev,next today",
                "center": "title",
                "right": "",
            },
        },
        key=f"schedule_calendar::{plan.id}::{version}",
    )

    if st.session_state.get(_SELECTED_DATE) is None:
        clicked = _clicked_date(component_value)
        if clicked:
            st.session_state[_SELECTED_DATE] = clicked
            st.session_state[_CALENDAR_INITIAL_DATE] = clicked

    selected_date = st.session_state.get(_SELECTED_DATE)
    if selected_date:
        _day_editor_dialog(settings, plan, selected_date, storage)


def _initial_template_defaults(settings: AppSettings) -> None:
    today = date.today()
    st.session_state.setdefault(_TEMPLATE_START, month_start(today))
    st.session_state.setdefault(_TEMPLATE_END, month_end(today))
    st.session_state.setdefault(_TEMPLATE_CYCLE_DAYS, 3)
    active_ids = [person.id for person in settings.people if person.is_active]
    if _TEMPLATE_PEOPLE not in st.session_state:
        st.session_state[_TEMPLATE_PEOPLE] = active_ids
    else:
        active_id_set = set(active_ids)
        selected = st.session_state[_TEMPLATE_PEOPLE]
        st.session_state[_TEMPLATE_PEOPLE] = [
            person_id for person_id in selected if person_id in active_id_set
        ]


def _template_cells() -> dict[tuple[int, str], str | None]:
    stored = st.session_state.get(_TEMPLATE_CELLS)
    return dict(stored) if isinstance(stored, dict) else {}


def _render_template_preview(
    preview: TemplatePreview,
    plan: SchedulePlan,
    request: TemplateRequest,
    storage: JsonStorage,
    people: dict[str, Person],
    shifts: dict[str, Shift],
) -> None:
    st.divider()
    st.markdown("#### 铺排预览")
    added_col, leave_col, skipped_col, rest_col = st.columns(4)
    added_col.metric("将新增", preview.added_count)
    leave_col.metric("请假跳过", preview.leave_skipped_count)
    skipped_col.metric("已有安排保留", preview.skipped_existing_count)
    rest_col.metric("休息格", preview.rest_cell_count)

    if preview.existing_violation_count:
        st.caption(
            f"当前方案原有 {preview.existing_violation_count} 个硬约束冲突；"
            "本次只统计新增冲突。"
        )

    if not preview.has_changes:
        st.info("所选范围内没有可补充的空白排班格，因此没有可应用的变化。")
        return

    rows = [
        {
            "日期": addition.date.isoformat(),
            "人员": getattr(people.get(addition.person_id), "name", addition.person_id),
            "班次": _shift_label(addition.shift_id, shifts),
        }
        for addition in preview.added
    ]
    max_rows = 200
    st.dataframe(
        pd.DataFrame(rows[:max_rows]),
        width="stretch",
        hide_index=True,
    )
    if len(rows) > max_rows:
        st.caption(f"仅展示前 {max_rows} 条，共 {len(rows)} 条新增安排。")

    acknowledged = True
    if preview.new_hard_violations:
        st.error(
            f"该模板会新增 {len(preview.new_hard_violations)} 个硬约束冲突。"
            "建议先调整周期顺序，例如在夜班后安排休息或中班。"
        )
        with st.expander("查看新增冲突", expanded=True):
            for violation in preview.new_hard_violations:
                st.markdown(f"- {violation.message}")
        acknowledged = st.checkbox(
            "我已知悉新增的硬约束冲突，仍要应用该模板",
            key=f"template_ack::{preview.request.signature()}",
        )

    apply_clicked = st.button(
        "应用到当前方案",
        type="primary",
        disabled=not acknowledged,
        width="stretch",
    )
    if not apply_clicked:
        return

    if not preview.matches(plan, request):
        st.session_state.pop(_TEMPLATE_PREVIEW, None)
        st.error("方案或模板已变化，请重新生成预览。")
        return

    try:
        persist_active_plan(storage, preview.candidate_plan)
    except StorageError as exc:
        st.error(f"应用模板失败：{exc}")
        return

    st.session_state.pop(_TEMPLATE_PREVIEW, None)
    set_flash(
        f"模板已应用：新增 {preview.added_count} 条排班，"
        f"跳过 {preview.leave_skipped_count} 个请假格，"
        f"保留 {preview.skipped_existing_count} 条已有安排。",
        "success",
    )
    st.rerun()


def _render_template_section(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    _initial_template_defaults(settings)
    active_people = [person for person in settings.people if person.is_active]
    active_shifts = [shift for shift in settings.shifts if shift.is_active]
    people = {person.id: person for person in active_people}
    shifts = {shift.id: shift for shift in active_shifts}

    with st.expander("周期模板铺排", expanded=False):
        st.caption(
            "定义 1–14 天循环矩阵，按日期区间重复铺排。"
            "只补充空白格，不覆盖已有排班和交接备注；已停用项不会参与新模板。"
        )
        if not active_people or not active_shifts:
            st.info("请先在基础配置中添加并启用人员和班次。")
            st.session_state.pop(_TEMPLATE_PREVIEW, None)
            return

        date_col, cycle_col = st.columns([2, 1])
        with date_col:
            start_date = st.date_input(
                "开始日期",
                key=_TEMPLATE_START,
                max_value=date(2100, 12, 31),
            )
            end_date = st.date_input(
                "结束日期",
                key=_TEMPLATE_END,
                max_value=date(2100, 12, 31),
            )
        with cycle_col:
            cycle_days = int(
                st.number_input(
                    "循环天数",
                    min_value=1,
                    max_value=TemplateService.MAX_CYCLE_DAYS,
                    step=1,
                    key=_TEMPLATE_CYCLE_DAYS,
                )
            )

        participants = st.multiselect(
            "参与人员",
            options=[person.id for person in active_people],
            format_func=lambda person_id: people[person_id].name,
            key=_TEMPLATE_PEOPLE,
        )
        if not participants:
            st.info("请至少选择一名参与人员。")
            st.session_state.pop(_TEMPLATE_PREVIEW, None)
            return
        if end_date < start_date:
            st.error("结束日期不能早于开始日期。")

        selected_people = [people[person_id] for person_id in participants]
        cells = _template_cells()
        valid_shift_ids = set(shifts)
        table = pd.DataFrame(
            {
                person.name: [
                    (
                        cells.get((day_index, person.id))
                        if cells.get((day_index, person.id)) in valid_shift_ids
                        else _REST_OPTION
                    )
                    for day_index in range(cycle_days)
                ]
                for person in selected_people
            },
            index=[f"第 {index + 1} 天" for index in range(cycle_days)],
        )
        table.index.name = "周期日"

        matrix_key_seed = "|".join(participants) + f"|{cycle_days}"
        matrix_key = "template_matrix::" + hashlib.sha1(
            matrix_key_seed.encode("utf-8")
        ).hexdigest()[:12]
        edited_table = st.data_editor(
            table,
            width="stretch",
            hide_index=False,
            num_rows="fixed",
            height=min(560, 84 + 36 * cycle_days),
            column_config={
                person.name: st.column_config.SelectboxColumn(
                    person.name,
                    required=True,
                    options=[_REST_OPTION, *shifts.keys()],
                    format_func=lambda value: _format_shift_option(value, shifts),
                )
                for person in selected_people
            },
            key=matrix_key,
        )

        latest_cells = _template_cells()
        for day_index, row_label in enumerate(edited_table.index):
            for person in selected_people:
                value = edited_table.at[row_label, person.name]
                if value == _REST_OPTION:
                    latest_cells[(day_index, person.id)] = None
                elif value in shifts:
                    latest_cells[(day_index, person.id)] = str(value)
        st.session_state[_TEMPLATE_CELLS] = latest_cells

        cycle = tuple(
            tuple(latest_cells.get((day_index, person_id)) for person_id in participants)
            for day_index in range(cycle_days)
        )
        request = TemplateRequest(
            start_date=start_date,
            end_date=end_date,
            person_ids=tuple(participants),
            cycle=cycle,
        )

        existing_preview = st.session_state.get(_TEMPLATE_PREVIEW)
        if isinstance(existing_preview, TemplatePreview) and not existing_preview.matches(
            plan, request
        ):
            st.session_state.pop(_TEMPLATE_PREVIEW, None)

        if st.button("生成预览", width="stretch"):
            try:
                preview = TemplateService.build_preview(settings, plan, request)
            except TemplateServiceError as exc:
                st.session_state.pop(_TEMPLATE_PREVIEW, None)
                st.error(f"无法生成预览：{exc}")
            else:
                st.session_state[_TEMPLATE_PREVIEW] = preview

        preview = st.session_state.get(_TEMPLATE_PREVIEW)
        if (
            isinstance(preview, TemplatePreview)
            and preview.matches(plan, request)
            and end_date >= start_date
        ):
            _render_template_preview(
                preview,
                plan,
                request,
                storage,
                people,
                shifts,
            )


def render(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    people = {person.id: person for person in settings.people}
    shifts = {shift.id: shift for shift in settings.shifts}

    events: list[dict[str, object]] = []
    for day in sorted(plan.days, key=lambda item: item.date):
        for assignment in day.assignments:
            person_name = getattr(
                people.get(assignment.person_id), "name", assignment.person_id
            )
            shift_name = _shift_label(assignment.shift_id, shifts)
            color = getattr(shifts.get(assignment.shift_id), "color", "#94A3B8")
            events.append(
                {
                    "id": f"{day.date.isoformat()}::{assignment.person_id}",
                    "title": f"{person_name} · {shift_name}",
                    "start": day.date.isoformat(),
                    "allDay": True,
                    "backgroundColor": color,
                    "borderColor": color,
                    "textColor": _text_color(color),
                }
            )

    first_col, second_col, third_col, fourth_col = st.columns(4)
    first_col.metric("已安排人次", sum(len(day.assignments) for day in plan.days))
    second_col.metric(
        "涉及人员",
        len(
            {
                assignment.person_id
                for day in plan.days
                for assignment in day.assignments
            }
        ),
    )
    third_col.metric("有交接备注的天数", sum(1 for day in plan.days if day.note))
    fourth_col.metric("请假记录", len(settings.leaves))

    violations = ConstraintService.find_hard_violations(settings, plan)
    _render_constraint_banner(violations)

    coverage = CoverageService.build_report(settings, plan)
    if coverage.has_shortages:
        st.warning(
            f"缺岗预警：已有安排的日期中有 {len(coverage.shortages)} 个"
            f"班次低于最低在岗人数，总缺口 {coverage.total_gap} 人。"
        )
        with st.expander("查看缺岗明细", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "日期": item.date.isoformat(),
                            "班次": item.shift_name,
                            "最低人数": item.required,
                            "已排人数": item.assigned,
                            "缺口": item.shortage,
                        }
                        for item in coverage.shortages
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

    if not events:
        st.info(
            "当前方案还没有排班。可以点击任意日期手动安排，"
            "或在下方使用周期模板批量铺排。"
        )

    _render_calendar(settings, plan, storage, events)
    with st.expander("查看排班明细", expanded=False):
        rows = [
            {
                "日期": day.date.isoformat(),
                "人员": getattr(
                    people.get(assignment.person_id),
                    "name",
                    assignment.person_id,
                ),
                "班次": _shift_label(assignment.shift_id, shifts),
                "交接备注": day.note,
            }
            for day in sorted(plan.days, key=lambda item: item.date)
            for assignment in day.assignments
        ]
        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("暂无明细。")

    _render_template_section(settings, plan, storage)
