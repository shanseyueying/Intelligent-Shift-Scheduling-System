"""Tab 4：人员、班次、请假配置。

人员与班次只做软归档/恢复，不物理删除；请假记录可以在确认弹窗中删除。
所有变更先经过 ConfigService 和 Pydantic 校验，再写入 settings.json。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.models import AppSettings, SchedulePlan
from src.services.config_service import ConfigService, ConfigServiceError
from src.services.constraint_service import ConstraintService
from src.services.csv_service import (
    CsvExportError,
    CsvImportError,
    CsvImportPreview,
    CsvService,
)
from src.services.leave_service import LeaveService
from src.services.plan_service import PlanService, PlanServiceError
from src.ui.state import activate_plan, persist_settings, set_flash
from src.utils.storage import JsonStorage, StorageError

_DIALOG_STATE = "config.dialog"
_DEFAULT_SHIFT_COLOR = "#3B82F6"
_CSV_PREVIEW = "csv.preview"
_CSV_NAME = "csv.plan_name"
_CSV_UPLOAD_VERSION = "csv.uploader_version"
_CSV_PREVIEW_DAYS = 30
_CSV_PREVIEW_VIOLATIONS = 20


def _open_dialog(kind: str, entity_id: str | None = None) -> None:
    st.session_state[_DIALOG_STATE] = {"kind": kind, "id": entity_id}
    st.rerun()


def _dismiss_dialog() -> None:
    st.session_state.pop(_DIALOG_STATE, None)


def _close_dialog() -> None:
    st.session_state.pop(_DIALOG_STATE, None)
    st.rerun()


def _shift_labels(settings: AppSettings) -> dict[str, str]:
    return {
        shift.id: shift.name + ("（已停用）" if not shift.is_active else "")
        for shift in settings.shifts
    }


def _format_shift_option(shift_id: str, labels: dict[str, str]) -> str:
    return labels.get(shift_id, f"未知班次({shift_id})")


def _person_names(settings: AppSettings) -> dict[str, str]:
    return {person.id: person.name for person in settings.people}


@st.dialog("人员配置", on_dismiss=_dismiss_dialog)
def _person_dialog(
    settings: AppSettings,
    storage: JsonStorage,
    person_id: str | None,
) -> None:
    person = next(
        (item for item in settings.people if item.id == person_id),
        None,
    )
    if person_id is not None and person is None:
        st.error("人员不存在，可能已被其他操作修改。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return

    st.markdown("#### 编辑人员" if person else "#### 新增人员")
    labels = _shift_labels(settings)

    with st.form(f"person_form::{person_id or 'new'}"):
        name = st.text_input("姓名", value=person.name if person else "")
        preferred = st.multiselect(
            "偏好班次（可选）",
            options=[shift.id for shift in settings.shifts],
            default=person.preferred_shift_ids if person else [],
            format_func=lambda shift_id: _format_shift_option(shift_id, labels),
        )
        submitted = st.form_submit_button("保存", type="primary", width="stretch")

    if st.button("取消", width="stretch"):
        _close_dialog()

    if not submitted:
        return

    try:
        if person:
            updated = ConfigService.update_person(
                settings,
                person.id,
                name=name,
                preferred_shift_ids=preferred,
            )
            success_message = f"已更新人员“{name.strip()}”。"
        else:
            updated = ConfigService.add_person(
                settings,
                name=name,
                preferred_shift_ids=preferred,
            )
            success_message = f"已新增人员“{name.strip()}”。"
        persist_settings(storage, updated)
    except (ConfigServiceError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    set_flash(success_message, "success")
    _close_dialog()


@st.dialog("班次配置", on_dismiss=_dismiss_dialog)
def _shift_dialog(
    settings: AppSettings,
    storage: JsonStorage,
    shift_id: str | None,
) -> None:
    shift = next(
        (item for item in settings.shifts if item.id == shift_id),
        None,
    )
    if shift_id is not None and shift is None:
        st.error("班次不存在，可能已被其他操作修改。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return

    st.markdown("#### 编辑班次" if shift else "#### 新增班次")
    with st.form(f"shift_form::{shift_id or 'new'}"):
        name = st.text_input("班次名称", value=shift.name if shift else "")
        color = st.color_picker(
            "颜色",
            value=shift.color if shift else _DEFAULT_SHIFT_COLOR,
        )
        load_weight = st.number_input(
            "负荷权重",
            min_value=0.1,
            max_value=10.0,
            value=float(shift.load_weight) if shift else 1.0,
            step=0.1,
            help="用于加权负荷统计；夜班等更耗神的班次可以设得更高。",
        )
        min_staff = st.number_input(
            "最低在岗人数",
            min_value=0,
            max_value=99,
            value=int(shift.min_staff) if shift else 1,
            step=1,
            help=(
                "未加入启用中的覆盖组时，按此班次单独检查；加入覆盖组后"
                "以组最低人数为准。0 表示不检查，不参与健康分。"
            ),
        )
        is_early = st.checkbox("属于早班", value=shift.is_early if shift else False)
        is_night = st.checkbox("属于夜班", value=shift.is_night if shift else False)
        submitted = st.form_submit_button("保存", type="primary", width="stretch")

    if st.button("取消", width="stretch"):
        _close_dialog()

    if not submitted:
        return

    try:
        if shift:
            updated = ConfigService.update_shift(
                settings,
                shift.id,
                name=name,
                color=color,
                load_weight=float(load_weight),
                min_staff=int(min_staff),
                is_early=is_early,
                is_night=is_night,
            )
            success_message = f"已更新班次“{name.strip()}”。"
        else:
            updated = ConfigService.add_shift(
                settings,
                name=name,
                color=color,
                load_weight=float(load_weight),
                min_staff=int(min_staff),
                is_early=is_early,
                is_night=is_night,
            )
            success_message = f"已新增班次“{name.strip()}”。"
        persist_settings(storage, updated)
    except (ConfigServiceError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    set_flash(success_message, "success")
    _close_dialog()


@st.dialog("缺岗覆盖组", on_dismiss=_dismiss_dialog)
def _coverage_group_dialog(
    settings: AppSettings,
    storage: JsonStorage,
    group_id: str | None,
) -> None:
    group = next(
        (item for item in settings.coverage_groups if item.id == group_id),
        None,
    )
    if group_id is not None and group is None:
        st.error("覆盖组不存在，可能已被其他操作修改。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return
    if not settings.shifts:
        st.info("请先添加班次，再配置覆盖组。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return

    st.markdown("#### 编辑覆盖组" if group else "#### 新增覆盖组")
    labels = _shift_labels(settings)
    with st.form(f"coverage_group_form::{group_id or 'new'}"):
        name = st.text_input(
            "覆盖组名称",
            value=group.name if group else "",
            help="例如“日班组”或“夜班组”。同一启用组内不能有重复班次。",
        )
        min_staff = st.number_input(
            "组最低人数",
            min_value=0,
            max_value=99,
            value=int(group.min_staff) if group else 1,
            step=1,
            help="组内所有成员的排班人数合并计算；0 表示暂不检查该组。",
        )
        shift_ids = st.multiselect(
            "组内班次",
            options=[shift.id for shift in settings.shifts],
            default=group.shift_ids if group else [],
            format_func=lambda shift_id: labels.get(
                shift_id, f"未知班次({shift_id})"
            ),
            help="例如把白班、早班、中班放进“日班组”，排其中任意班次都计入日班组人数。",
        )
        submitted = st.form_submit_button(
            "保存",
            type="primary",
            width="stretch",
        )

    st.caption(
        "组内任一班次的人数会合并计算；未加入启用组的班次仍按自己的最低人数检查。"
        "一个班次只能属于一个启用中的覆盖组。"
    )
    if st.button("取消", width="stretch"):
        _close_dialog()

    if not submitted:
        return

    try:
        if group:
            updated = ConfigService.update_coverage_group(
                settings,
                group.id,
                name=name,
                min_staff=int(min_staff),
                shift_ids=shift_ids,
            )
            success_message = f"已更新覆盖组“{name.strip()}”。"
        else:
            updated = ConfigService.add_coverage_group(
                settings,
                name=name,
                min_staff=int(min_staff),
                shift_ids=shift_ids,
            )
            success_message = f"已新增覆盖组“{name.strip()}”。"
        persist_settings(storage, updated)
    except (ConfigServiceError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    set_flash(success_message, "success")
    _close_dialog()


@st.dialog("新增请假", on_dismiss=_dismiss_dialog)
def _leave_dialog(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    st.markdown("#### 新增请假记录")
    active_people = [person for person in settings.people if person.is_active]
    if not active_people:
        st.info("当前没有启用中的人员，无法新增请假。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return

    person_labels = {person.id: person.name for person in active_people}
    with st.form("leave_form::new"):
        person_id = st.selectbox(
            "请假人员",
            options=[person.id for person in active_people],
            format_func=lambda value: person_labels.get(value, "未知人员"),
        )
        start_date = st.date_input("开始日期", value=date.today())
        end_date = st.date_input("结束日期", value=date.today())
        reason = st.text_input("原因（可选）")
        submitted = st.form_submit_button("保存", type="primary", width="stretch")

    st.caption("新排班会自动避开请假；已有冲突不会静默删除，会在方案健康度中提示。")
    if st.button("取消", width="stretch"):
        _close_dialog()

    if not submitted:
        return

    try:
        updated = ConfigService.add_leave(
            settings,
            person_id=person_id,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
        )
        persist_settings(storage, updated)
    except (ConfigServiceError, StorageError) as exc:
        st.error(f"保存失败：{exc}")
        return

    conflicts = LeaveService.find_conflicts(updated, plan)
    if conflicts:
        set_flash(
            f"已新增请假；当前方案有 {len(conflicts)} 条安排与请假冲突，"
            "请在方案健康度中确认清理。",
            "warning",
        )
    else:
        set_flash("已新增请假记录。", "success")
    _close_dialog()


@st.dialog("删除请假", on_dismiss=_dismiss_dialog)
def _delete_leave_dialog(
    settings: AppSettings,
    storage: JsonStorage,
    leave_id: str,
) -> None:
    leave = next((item for item in settings.leaves if item.id == leave_id), None)
    if leave is None:
        st.error("请假记录不存在。")
        if st.button("关闭", width="stretch"):
            _close_dialog()
        return

    person_name = _person_names(settings).get(leave.person_id, leave.person_id)
    st.warning(
        f"确定删除 {person_name} 的请假记录吗？\n\n"
        f"{leave.start_date.isoformat()} 至 {leave.end_date.isoformat()}"
        + (f"\n\n原因：{leave.reason}" if leave.reason else "")
    )
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("确认删除", type="primary", width="stretch"):
        try:
            updated = ConfigService.delete_leave(settings, leave.id)
            persist_settings(storage, updated)
        except (ConfigServiceError, StorageError) as exc:
            st.error(f"删除失败：{exc}")
            return
        set_flash("已删除请假记录。", "success")
        _close_dialog()
    if cancel_col.button("取消", width="stretch"):
        _close_dialog()


def _render_people(settings: AppSettings, storage: JsonStorage) -> None:
    title_col, add_col = st.columns([4, 1])
    title_col.markdown("#### 人员")
    if add_col.button("新增人员", width="stretch"):
        _open_dialog("person", None)

    if not settings.people:
        st.caption("尚未添加人员。")
        return

    shift_names = _shift_labels(settings)
    for person in settings.people:
        name_col, status_col, pref_col, edit_col, active_col = st.columns(
            [2.2, 1, 4, 1, 1]
        )
        name_col.markdown(f"**{person.name}**")
        if person.is_active:
            status_col.markdown(":green[启用]")
        else:
            status_col.markdown(":gray[已停用]")
        preferences = "、".join(
            shift_names.get(shift_id, f"未知({shift_id})")
            for shift_id in person.preferred_shift_ids
        )
        pref_col.caption(preferences or "暂无偏好")

        if edit_col.button("编辑", key=f"edit_person::{person.id}", width="stretch"):
            _open_dialog("person", person.id)

        toggle_label = "停用" if person.is_active else "恢复"
        if active_col.button(
            toggle_label,
            key=f"toggle_person::{person.id}",
            width="stretch",
        ):
            try:
                updated = ConfigService.set_person_active(
                    settings,
                    person.id,
                    not person.is_active,
                )
                persist_settings(storage, updated)
            except (ConfigServiceError, StorageError) as exc:
                st.error(f"操作失败：{exc}")
            else:
                set_flash(
                    f"已{'停用' if person.is_active else '恢复'}人员“{person.name}”。",
                    "info",
                )
                st.rerun()


def _render_shifts(settings: AppSettings, storage: JsonStorage) -> None:
    title_col, add_col = st.columns([4, 1])
    title_col.markdown("#### 班次")
    if add_col.button("新增班次", width="stretch"):
        _open_dialog("shift", None)

    if not settings.shifts:
        st.caption("尚未添加班次。")
        return

    type_names = {
        (False, False): "普通",
        (True, False): "早班",
        (False, True): "夜班",
    }
    group_by_shift = {
        shift_id: group.name
        for group in settings.coverage_groups
        if group.is_active
        for shift_id in group.shift_ids
    }
    for shift in settings.shifts:
        (
            name_col,
            status_col,
            type_col,
            weight_col,
            staffing_col,
            edit_col,
            active_col,
        ) = st.columns([2.2, 1, 1.1, 1, 1, 1, 1])
        name_col.markdown(
            "<span style='display:inline-block;width:11px;height:11px;"
            "border-radius:50%;margin-right:7px;"
            f"background:{shift.color};'></span>**{shift.name}**",
            unsafe_allow_html=True,
        )
        if shift.is_active:
            status_col.markdown(":green[启用]")
        else:
            status_col.markdown(":gray[已停用]")
        type_col.caption(
            type_names.get((shift.is_early, shift.is_night), "异常")
        )
        weight_col.caption(f"权重 {shift.load_weight:g}")
        if shift.id in group_by_shift:
            staffing_col.caption(f"组：{group_by_shift[shift.id]}")
        else:
            staffing_col.caption(f"最低 {shift.min_staff} 人")

        if edit_col.button("编辑", key=f"edit_shift::{shift.id}", width="stretch"):
            _open_dialog("shift", shift.id)

        toggle_label = "停用" if shift.is_active else "恢复"
        if active_col.button(
            toggle_label,
            key=f"toggle_shift::{shift.id}",
            width="stretch",
        ):
            try:
                updated = ConfigService.set_shift_active(
                    settings,
                    shift.id,
                    not shift.is_active,
                )
                persist_settings(storage, updated)
            except (ConfigServiceError, StorageError) as exc:
                st.error(f"操作失败：{exc}")
            else:
                set_flash(
                    f"已{'停用' if shift.is_active else '恢复'}班次“{shift.name}”。",
                    "info",
                )
                st.rerun()


def _render_coverage_groups(
    settings: AppSettings,
    storage: JsonStorage,
) -> None:
    title_col, add_col = st.columns([4, 1])
    title_col.markdown("#### 缺岗覆盖组")
    if add_col.button(
        "新增覆盖组",
        width="stretch",
        disabled=not settings.shifts,
    ):
        _open_dialog("coverage_group", None)
    st.caption(
        "把可以互相替代的班次放进同一个覆盖组。组内所有成员的排班人数合并计算，"
        "达到组最低人数即视为该组不缺岗；未入组班次仍按自己的最低人数检查。"
    )

    if not settings.coverage_groups:
        st.caption("尚未配置覆盖组，当前按每个班次单独检查缺岗。")
        return

    shift_labels = _shift_labels(settings)
    for group in settings.coverage_groups:
        (
            name_col,
            status_col,
            members_col,
            minimum_col,
            edit_col,
            active_col,
        ) = st.columns([2.2, 1, 4, 1.1, 1, 1])
        name_col.markdown(f"**{group.name}**")
        if group.is_active:
            status_col.markdown(":green[启用]")
        else:
            status_col.markdown(":gray[已停用]")
        members = "、".join(
            shift_labels.get(shift_id, f"未知({shift_id})")
            for shift_id in group.shift_ids
        )
        members_col.caption(members or "未配置成员")
        minimum_col.caption(
            "不检查" if group.min_staff == 0 else f"最低 {group.min_staff} 人"
        )

        if edit_col.button(
            "编辑",
            key=f"edit_coverage_group::{group.id}",
            width="stretch",
        ):
            _open_dialog("coverage_group", group.id)

        toggle_label = "停用" if group.is_active else "恢复"
        if active_col.button(
            toggle_label,
            key=f"toggle_coverage_group::{group.id}",
            width="stretch",
        ):
            try:
                updated = ConfigService.set_coverage_group_active(
                    settings,
                    group.id,
                    not group.is_active,
                )
                persist_settings(storage, updated)
            except (ConfigServiceError, StorageError) as exc:
                st.error(f"操作失败：{exc}")
            else:
                set_flash(
                    f"已{'停用' if group.is_active else '恢复'}覆盖组"
                    f"“{group.name}”。",
                    "info",
                )
                st.rerun()


def _render_leaves(settings: AppSettings, storage: JsonStorage) -> None:
    title_col, add_col = st.columns([4, 1])
    title_col.markdown("#### 请假记录")
    if add_col.button("新增请假", width="stretch"):
        _open_dialog("leave", None)
    st.caption(
        "新排班与周期模板会自动避开请假；已有冲突在方案健康度中提示并确认清理。"
    )

    if not settings.leaves:
        st.caption("暂无请假记录。")
        return

    person_names = _person_names(settings)
    for leave in sorted(
        settings.leaves,
        key=lambda item: (item.start_date, item.person_id),
    ):
        person_col, range_col, reason_col, delete_col = st.columns([2, 2.5, 4, 1])
        person_col.markdown(f"**{person_names.get(leave.person_id, leave.person_id)}**")
        range_col.caption(
            f"{leave.start_date.isoformat()} 至 {leave.end_date.isoformat()}"
        )
        reason_col.caption(leave.reason or "未填写原因")
        if delete_col.button(
            "删除",
            key=f"delete_leave::{leave.id}",
            width="stretch",
        ):
            _open_dialog("delete_leave", leave.id)


def _csv_upload_key() -> str:
    version = int(st.session_state.get(_CSV_UPLOAD_VERSION, 0))
    return f"csv.uploader::{version}"


def _reset_csv_upload() -> None:
    """换一个上传控件实例，确保导入成功后浏览器里的文件也被清空。

    这里不改 _CSV_NAME：它对应的 text_input 在本轮已经实例化，
    下一轮空上传分支会在组件实例化前清理它。
    """
    st.session_state[_CSV_UPLOAD_VERSION] = (
        int(st.session_state.get(_CSV_UPLOAD_VERSION, 0)) + 1
    )
    st.session_state.pop(_CSV_PREVIEW, None)


def _suggest_csv_plan_name(
    source_name: str,
    plans: list[SchedulePlan],
) -> str:
    base = CsvService.suggested_plan_name(source_name) or "CSV 导入"
    existing = {plan.name.casefold() for plan in plans}
    if base.casefold() not in existing:
        return base
    index = 2
    while f"{base} {index}".casefold() in existing:
        index += 1
    return f"{base} {index}"


def _preview_frame(
    settings: AppSettings,
    days,
) -> pd.DataFrame:
    shifts_by_id = {shift.id: shift for shift in settings.shifts}
    referenced = {
        assignment.person_id
        for day in days
        for assignment in day.assignments
    }
    people = [
        person for person in settings.people if person.id in referenced
    ]
    rows: list[dict[str, str]] = []
    for day in days[:_CSV_PREVIEW_DAYS]:
        by_person = {
            assignment.person_id: assignment.shift_id
            for assignment in day.assignments
        }
        row: dict[str, str] = {"日期": day.date.isoformat()}
        for person in people:
            shift_id = by_person.get(person.id)
            row[person.name] = (
                shifts_by_id[shift_id].name
                if shift_id in shifts_by_id
                else ("" if shift_id is None else f"未知班次({shift_id})")
            )
        row["交接备注"] = day.note
        rows.append(row)
    return pd.DataFrame(rows)


def _violation_frame(
    settings: AppSettings,
    preview: CsvImportPreview,
) -> pd.DataFrame:
    people = {person.id: person.name for person in settings.people}
    labels = {
        ConstraintService.NIGHT_THEN_EARLY: "夜班后次日早班",
        ConstraintService.LEAVE_CONFLICT: "请假冲突",
    }
    rows = [
        {
            "类型": labels.get(violation.code, violation.code),
            "日期": violation.date.isoformat(),
            "人员": people.get(violation.person_id, violation.person_id),
            "说明": violation.message,
        }
        for violation in preview.hard_violations[:_CSV_PREVIEW_VIOLATIONS]
    ]
    return pd.DataFrame(rows)


def _render_csv_export(settings: AppSettings, plan: SchedulePlan) -> None:
    st.markdown("#### 导出当前方案")
    st.caption(
        "宽表格式：第一列为日期，中间每列一名人员，最后一列为交接备注；"
        "空白表示休息。导出文件带 UTF-8 BOM，可直接用 Excel 打开。"
    )
    try:
        data, filename = CsvService.export(settings, plan)
    except CsvExportError as exc:
        st.error(f"无法导出当前方案：{exc}")
        return
    st.download_button(
        "下载 CSV",
        data=data,
        file_name=filename,
        mime="text/csv",
        key="csv.export_current_plan",
        width="stretch",
    )
    st.caption(f"文件名：{filename}")


def _render_csv_import(settings: AppSettings, storage: JsonStorage) -> None:
    st.markdown("#### 导入为新方案")
    st.caption(
        "上传后会先做结构与引用校验，通过后才允许导入。导入只会创建新方案，"
        "不会覆盖当前方案；空白或“休息”表示休息。"
    )
    with st.expander("CSV 格式说明", expanded=False):
        st.markdown(
            "- 必需列：`日期`（`YYYY-MM-DD`）。\n"
            "- 人员列可写成 `人员:张三`（推荐，与导出格式一致），"
            "也可直接写已在基础配置中的姓名。\n"
            "- 可选列：`交接备注`，最多 1000 字。\n"
            "- 单元格填写班次名称；留空或填“休息”表示休息。\n"
            "- 支持 UTF-8（推荐）及 GB18030；文件不超过 2 MB、数据不超过 5000 行。\n"
            "- 已停用人员/班次允许导入但会警告；硬约束违规也会作为警告展示，不阻止建方案。"
        )

    uploaded = st.file_uploader(
        "上传 CSV（最大 2 MB）",
        type=["csv"],
        key=_csv_upload_key(),
    )
    if uploaded is None:
        st.session_state.pop(_CSV_PREVIEW, None)
        st.session_state.pop(_CSV_NAME, None)
        st.info("选择 CSV 后，这里会显示校验结果、警告和数据预览。")
        return

    source_name = Path(uploaded.name).name
    try:
        raw = uploaded.getvalue()
    except OSError as exc:
        st.error(f"读取上传文件失败：{exc}")
        return

    digest = CsvService.file_digest(raw)
    settings_digest = CsvService.settings_digest(settings)
    cached = st.session_state.get(_CSV_PREVIEW)
    needs_parse = not (
        isinstance(cached, dict)
        and cached.get("digest") == digest
        and cached.get("settings_digest") == settings_digest
    )
    if needs_parse:
        plans = storage.list_plans().plans
        try:
            preview = CsvService.parse(
                settings,
                raw,
                source_name=source_name,
            )
        except CsvImportError as exc:
            st.session_state[_CSV_PREVIEW] = {
                "digest": digest,
                "settings_digest": settings_digest,
                "source_name": source_name,
                "errors": exc.errors,
            }
            st.session_state.pop(_CSV_NAME, None)
            cached = st.session_state[_CSV_PREVIEW]
        else:
            st.session_state[_CSV_PREVIEW] = {
                "digest": digest,
                "settings_digest": settings_digest,
                "source_name": source_name,
                "preview": preview,
            }
            st.session_state[_CSV_NAME] = _suggest_csv_plan_name(
                source_name,
                plans,
            )
            cached = st.session_state[_CSV_PREVIEW]

    if not isinstance(cached, dict):
        st.error("CSV 预览状态已失效，请重新选择文件。")
        return

    errors = cached.get("errors")
    if errors:
        st.error(f"“{source_name}”校验失败，未创建任何方案。")
        for message in errors:
            st.markdown(f"- {message}")
        return

    preview = cached.get("preview")
    if preview is None:
        st.error("CSV 预览状态已失效，请重新选择文件。")
        return

    st.success(
        f"校验通过：{preview.day_count} 天、"
        f"{preview.assignment_count} 条排班。"
    )
    for warning in preview.warnings:
        st.warning(warning)

    if preview.hard_violations:
        st.markdown(
            f"**硬约束违规（前 {_CSV_PREVIEW_VIOLATIONS} 条）**"
        )
        if len(preview.hard_violations) > _CSV_PREVIEW_VIOLATIONS:
            st.caption(
                f"共 {len(preview.hard_violations)} 条，"
                f"另有 {len(preview.hard_violations) - _CSV_PREVIEW_VIOLATIONS} 条未显示。"
            )
        st.dataframe(
            _violation_frame(settings, preview),
            width="stretch",
            hide_index=True,
        )
        st.caption("这些违规会让新方案健康分为“不合格”，但仍可导入后统一修复。")

    st.markdown("**数据预览**")
    st.dataframe(
        _preview_frame(settings, preview.plan.days),
        width="stretch",
        hide_index=True,
    )
    if preview.day_count > _CSV_PREVIEW_DAYS:
        st.caption(
            f"仅显示前 {_CSV_PREVIEW_DAYS} 天，"
            f"导入后会包含全部 {preview.day_count} 天。"
        )
    st.caption("空白单元格表示休息；已停用人员列以原姓名展示。")

    plans = storage.list_plans().plans
    if _CSV_NAME not in st.session_state:
        st.session_state[_CSV_NAME] = _suggest_csv_plan_name(
            source_name,
            plans,
        )
    name = st.text_input(
        "新方案名称",
        key=_CSV_NAME,
        max_chars=50,
        help="导入后会创建一个独立的新方案并自动切换过去，原方案保持不变。",
    )
    name_error: str | None = None
    try:
        PlanService.ensure_unique_name(name, plans)
    except PlanServiceError as exc:
        name_error = str(exc)
    if name_error:
        st.caption(f"⚠️ {name_error}")

    if st.button(
        "导入为新方案",
        type="primary",
        key=f"csv.import::{digest[:12]}",
        disabled=name_error is not None,
        width="stretch",
    ):
        try:
            PlanService.ensure_unique_name(
                name,
                storage.list_plans().plans,
            )
            imported_plan = PlanService.rename_plan(preview.plan, name)
            storage.save_plan(imported_plan)
        except (PlanServiceError, ValidationError, StorageError) as exc:
            st.error(f"导入失败，现有方案未被修改：{exc}")
            return

        try:
            activate_plan(storage, settings, imported_plan)
        except (ValidationError, StorageError) as exc:
            st.error(
                f"排班已保存为新方案“{name.strip()}”，但切换活动方案失败：{exc}。"
                "请到“方案对比与打分”中手动切换。"
            )
            return

        _reset_csv_upload()
        set_flash(
            f"已导入为新方案“{imported_plan.name}”，"
            f"共 {len(imported_plan.days)} 天。",
            "success",
        )
        st.rerun()


def _render_csv_section(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    st.markdown("#### CSV 导入导出")
    st.caption(
        "CSV 用于批量交接和跨工具编辑；导入始终创建独立新方案，"
        "原方案与历史备份不受影响。"
    )
    _render_csv_export(settings, plan)
    st.divider()
    _render_csv_import(settings, storage)


def _dispatch_dialog(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    action = st.session_state.get(_DIALOG_STATE)
    if not isinstance(action, dict):
        st.session_state.pop(_DIALOG_STATE, None)
        return

    kind = action.get("kind")
    entity_id = action.get("id")
    if kind == "person":
        _person_dialog(settings, storage, entity_id)
    elif kind == "shift":
        _shift_dialog(settings, storage, entity_id)
    elif kind == "coverage_group":
        _coverage_group_dialog(settings, storage, entity_id)
    elif kind == "leave":
        _leave_dialog(settings, plan, storage)
    elif kind == "delete_leave" and entity_id:
        _delete_leave_dialog(settings, storage, entity_id)
    else:
        st.session_state.pop(_DIALOG_STATE, None)


def render(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    st.subheader("基础配置")
    st.caption(
        "人员与班次采用软归档：停用后不再参与新排班，但历史方案中的引用保留，"
        "可随时恢复。"
    )
    active_people = sum(1 for person in settings.people if person.is_active)
    active_shifts = sum(1 for shift in settings.shifts if shift.is_active)
    active_groups = sum(
        1 for group in settings.coverage_groups if group.is_active
    )
    st.caption(
        f"当前方案“{plan.name}” · "
        f"启用人员 {active_people}/{len(settings.people)} · "
        f"启用班次 {active_shifts}/{len(settings.shifts)} · "
        f"启用覆盖组 {active_groups}/{len(settings.coverage_groups)} · "
        f"请假记录 {len(settings.leaves)}"
    )

    _render_people(settings, storage)
    st.divider()
    _render_shifts(settings, storage)
    st.divider()
    _render_coverage_groups(settings, storage)
    st.divider()
    _render_leaves(settings, storage)
    st.divider()
    _render_csv_section(settings, plan, storage)
    _dispatch_dialog(settings, plan, storage)
