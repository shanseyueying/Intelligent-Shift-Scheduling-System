"""独立月历模块：FullCalendar 渲染、日期点击和当天排班弹窗。

日历单独成文件，避免模板铺排、缺岗明细等页面内容与第三方组件状态耦合。
只有在日历 Tab 处于激活状态时才挂载 FullCalendar，防止隐藏 Tab 中用零宽
容器初始化组件，切回日历时出现空白月历。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError
from streamlit_calendar import calendar

from src.models import AppSettings, SchedulePlan, Shift
from src.services.constraint_service import ConstraintService
from src.services.schedule_service import ScheduleService, ScheduleServiceError
from src.ui.state import persist_active_plan, set_flash
from src.utils.dates import local_date_from_iso
from src.utils.storage import JsonStorage, StorageError

REST_OPTION = "__REST__"
UNSCHEDULED_OPTION = "__UNSCHEDULED__"
_CALENDAR_VERSION = "calendar.version"
_CALENDAR_INITIAL_DATE = "calendar.initial_date"
_SELECTED_DATE = "calendar.selected_date"
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _text_color(hex_color: str) -> str:
    color = hex_color.lstrip("#")
    red, green, blue = (int(color[index : index + 2], 16) for index in (0, 2, 4))
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#0F172A" if luminance > 0.66 else "#FFFFFF"


def shift_label(shift_id: str, shifts: dict[str, Shift]) -> str:
    shift = shifts.get(shift_id)
    return shift.name if shift else f"未知班次({shift_id})"


def format_shift_option(value: str, shifts: dict[str, Shift]) -> str:
    if value == UNSCHEDULED_OPTION:
        return "未安排"
    if value == REST_OPTION:
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
        "每位人员最多一个班次；“未安排”可被模板补充，“休息”会锁定该人当天，"
        "后续模板不会自动填充。请假、已停用人员/班次的历史安排会自动保留。"
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
                "班次": shift_label(assignment.shift_id, shifts),
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
    rest_people = set(edit_view.rest_person_ids)
    version = int(st.session_state.get(_CALENDAR_VERSION, 0))
    editor_key = f"day_editor::{plan.id}::{on_date.isoformat()}::{version}"
    edited = None

    def initial_value(person_id: str) -> str:
        shift_id = edit_view.initial_shift_by_person.get(person_id)
        if shift_id:
            return shift_id
        if person_id in rest_people:
            return REST_OPTION
        return UNSCHEDULED_OPTION

    with st.form(f"day_form::{editor_key}", border=False):
        if editable_people:
            table = pd.DataFrame(
                {
                    "人员": [person.name for person in editable_people],
                    "班次": [
                        initial_value(person.id)
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
                        options=[
                            UNSCHEDULED_OPTION,
                            REST_OPTION,
                            *active_shifts.keys(),
                        ],
                        format_func=lambda value: format_shift_option(
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
    rest_ids = set(edit_view.preserved_rest_person_ids)
    if editable_people:
        if edited is None or set(edited.index) != {
            person.id for person in editable_people
        }:
            st.error("排班表格结构异常，请关闭弹窗后重试。")
            return
        for person_id in edited.index:
            value = edited.at[person_id, "班次"]
            if value in {REST_OPTION, UNSCHEDULED_OPTION}:
                selected[str(person_id)] = None
                if value == REST_OPTION:
                    rest_ids.add(str(person_id))
            elif value in active_shifts:
                selected[str(person_id)] = str(value)
            else:
                st.error("存在无法识别的班次选项，请重新选择。")
                return

    try:
        merged = edit_view.merge_selections(selected)
        day = ScheduleService.build_day(
            settings,
            on_date,
            merged,
            note,
            rest_person_ids=rest_ids,
        )
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
        st.session_state.pop("template.preview", None)
    except (ScheduleServiceError, ValidationError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    if new_violations:
        set_flash(
            f"已保存当天排班，但新增 {len(new_violations)} 个硬约束冲突，"
            "请查看日历下方的硬约束提示。",
            "warning",
        )
    else:
        set_flash(f"已保存 {on_date.isoformat()} 的排班。", "success")
    _close_day_editor()
    st.rerun()


def _build_events(
    settings: AppSettings,
    plan: SchedulePlan,
) -> list[dict[str, object]]:
    people = {person.id: person for person in settings.people}
    shifts = {shift.id: shift for shift in settings.shifts}
    events: list[dict[str, object]] = []
    for day in sorted(plan.days, key=lambda item: item.date):
        for assignment in day.assignments:
            person_name = getattr(
                people.get(assignment.person_id), "name", assignment.person_id
            )
            shift_name = shift_label(assignment.shift_id, shifts)
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
        for person_id in day.rest_person_ids:
            person_name = getattr(people.get(person_id), "name", person_id)
            events.append(
                {
                    "id": f"{day.date.isoformat()}::{person_id}::rest",
                    "title": f"{person_name} · 休息",
                    "start": day.date.isoformat(),
                    "allDay": True,
                    "backgroundColor": "#E2E8F0",
                    "borderColor": "#CBD5E1",
                    "textColor": "#475569",
                }
            )
    return events


def render(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
    *,
    is_active: bool,
) -> None:
    """渲染独立月历；只在 Tab 激活时挂载第三方日历组件。"""
    _ensure_calendar_state()
    if not is_active:
        return

    version = int(st.session_state.get(_CALENDAR_VERSION, 0))
    initial_date = st.session_state.get(_CALENDAR_INITIAL_DATE, date.today())
    component_value = calendar(
        events=_build_events(settings, plan),
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
