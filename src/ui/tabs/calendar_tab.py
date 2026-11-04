"""Tab 1：独立月历模块 + 日历下方的明细与周期模板铺排。

页面顺序固定为：月历 → 方案概览 → 硬约束/缺岗提示 → 缺岗与排班明细 →
周期模板。日历组件单独封装在 ``src.ui.calendar_view``，并且只在日历 Tab
激活时挂载，避免隐藏 Tab 中组件宽度为 0 导致切回后月历空白。
"""

from __future__ import annotations

import hashlib
from datetime import date

import pandas as pd
import streamlit as st

from src.models import AppSettings, Person, SchedulePlan, Shift
from src.services.constraint_service import ConstraintService, HardConstraintViolation
from src.services.coverage_service import CoverageReport, CoverageService
from src.services.template_service import (
    TemplatePreview,
    TemplateRequest,
    TemplateService,
    TemplateServiceError,
)
from src.ui.calendar_view import (
    REST_OPTION,
    format_shift_option,
    render as render_calendar,
    shift_label,
)
from src.ui.state import persist_active_plan, set_flash
from src.utils.dates import month_end, month_start
from src.utils.storage import JsonStorage, StorageError

_TEMPLATE_START = "template.start_date"
_TEMPLATE_END = "template.end_date"
_TEMPLATE_PEOPLE = "template.person_ids"
_TEMPLATE_CYCLE_DAYS = "template.cycle_days"
_TEMPLATE_CELLS = "template.cells"
_TEMPLATE_PREVIEW = "template.preview"


def _render_constraint_banner(
    violations: tuple[HardConstraintViolation, ...],
) -> None:
    if not violations:
        return
    st.warning(f"当前方案有 {len(violations)} 个硬约束冲突。")
    with st.expander("查看硬约束冲突明细", expanded=False):
        for violation in violations:
            st.markdown(f"- {violation.message}")


def _render_coverage_detail(coverage: CoverageReport) -> None:
    if not coverage.has_shortages:
        st.caption("所有已有安排的日期均满足覆盖组/班次的最低人数。")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "日期": item.date.isoformat(),
                    "覆盖项": item.display_name,
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
    st.caption("覆盖组内任一成员的排班人数会合并计算；停用组不参与检查。")


def _render_schedule_detail(
    settings: AppSettings,
    plan: SchedulePlan,
) -> None:
    people = {person.id: person for person in settings.people}
    shifts = {shift.id: shift for shift in settings.shifts}
    rows = [
        {
            "日期": day.date.isoformat(),
            "人员": getattr(
                people.get(assignment.person_id),
                "name",
                assignment.person_id,
            ),
            "班次": shift_label(assignment.shift_id, shifts),
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
            "班次": shift_label(addition.shift_id, shifts),
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
                        else REST_OPTION
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
                    options=[REST_OPTION, *shifts.keys()],
                    format_func=lambda value: format_shift_option(value, shifts),
                )
                for person in selected_people
            },
            key=matrix_key,
        )

        latest_cells = _template_cells()
        for day_index, row_label in enumerate(edited_table.index):
            for person in selected_people:
                value = edited_table.at[row_label, person.name]
                if value == REST_OPTION:
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
    *,
    is_active: bool = True,
) -> None:
    st.caption(
        "点击任意日期或已有事件，编辑当天全员排班；日历下方查看缺岗、明细和模板铺排。"
    )
    render_calendar(settings, plan, storage, is_active=is_active)

    if not any(day.assignments for day in plan.days):
        st.info(
            "当前方案还没有排班。可以点击任意日期手动安排，"
            "或在下方使用周期模板批量铺排。"
        )

    st.divider()
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
            f"缺岗预警：已有安排的日期中有 {len(coverage.shortages)} 项"
            f"最低人数要求未满足，总缺口 {coverage.total_gap} 人。"
        )

    st.markdown("#### 排班与缺岗明细")
    with st.expander("缺岗明细", expanded=coverage.has_shortages):
        _render_coverage_detail(coverage)
    with st.expander("排班明细", expanded=False):
        _render_schedule_detail(settings, plan)

    st.divider()
    _render_template_section(settings, plan, storage)
