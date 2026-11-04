"""Tab 2：多方案管理、健康评分、请假冲突清理、缺岗预警与方案差异对比。"""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from src.models import AppSettings, SchedulePlan
from src.services.comparison_service import ComparisonService
from src.services.constraint_service import ConstraintService
from src.services.coverage_service import CoverageReport, CoverageService
from src.services.leave_service import LeaveService
from src.services.plan_service import PlanService, PlanServiceError
from src.services.repair_service import RepairSearchResult, RepairService
from src.services.scoring_service import HealthResult, ScoringService
from src.services.swap_service import SwapRecommendation, SwapService, SwapServiceError
from src.ui.state import (
    activate_plan,
    clear_plan_view_state,
    persist_active_plan,
    set_active_plan,
    set_flash,
)
from src.utils.storage import JsonStorage, PlanInventory, StorageError

_PLAN_SELECTOR = "plan.selector"
_PLAN_SELECTOR_PENDING = "plan.selector.pending"
_PLAN_ACTION = "plan.action"
_LEAVE_CLEANUP = "plan.leave_cleanup"
_COMPARE_REFERENCE = "compare.reference_plan"
_SWAP_TARGET = "swap.target"
_SWAP_RESULTS = "swap.results"
_REPAIR_PREVIEW = "repair.preview"


def _open_plan_action(mode: str) -> None:
    st.session_state[_PLAN_ACTION] = {"mode": mode}
    st.rerun()


def _dismiss_plan_action() -> None:
    st.session_state.pop(_PLAN_ACTION, None)


def _close_plan_action() -> None:
    st.session_state.pop(_PLAN_ACTION, None)
    st.rerun()


def _dismiss_leave_cleanup() -> None:
    st.session_state.pop(_LEAVE_CLEANUP, None)


def _close_leave_cleanup() -> None:
    st.session_state.pop(_LEAVE_CLEANUP, None)
    st.rerun()


def _plan_label(plan: SchedulePlan) -> str:
    local_time = plan.updated_at.astimezone()
    return f"{plan.name} · {local_time.strftime('%m-%d %H:%M')}"


def _suggest_name(base: str, plans: list[SchedulePlan]) -> str:
    existing = {plan.name.casefold() for plan in plans}
    if base.casefold() not in existing:
        return base
    index = 2
    while f"{base} {index}".casefold() in existing:
        index += 1
    return f"{base} {index}"


@st.dialog("方案管理", on_dismiss=_dismiss_plan_action)
def _plan_action_dialog(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    action = st.session_state.get(_PLAN_ACTION)
    mode = action.get("mode") if isinstance(action, dict) else None
    if mode not in {"new", "copy", "rename"}:
        st.error("无法识别的方案操作。")
        if st.button("关闭", width="stretch"):
            _close_plan_action()
        return

    inventory = storage.list_plans()
    existing_plans = inventory.plans
    headings = {
        "new": "新建空白方案",
        "copy": "复制当前方案",
        "rename": "重命名当前方案",
    }
    defaults = {
        "new": _suggest_name("新方案", existing_plans),
        "copy": _suggest_name(f"{plan.name} 副本", existing_plans),
        "rename": plan.name,
    }
    st.markdown(f"#### {headings[mode]}")
    if mode == "copy":
        st.caption(f"将复制“{plan.name}”的全部排班、备注和审计记录。")
    elif mode == "rename":
        st.caption("只修改方案名称，不影响排班内容。")

    with st.form(f"plan_action_form::{mode}"):
        name = st.text_input("方案名称", value=defaults[mode])
        submitted = st.form_submit_button("确认", type="primary", width="stretch")

    if st.button("取消", width="stretch"):
        _close_plan_action()

    if not submitted:
        return

    try:
        PlanService.ensure_unique_name(
            name,
            existing_plans,
            exclude_plan_id=plan.id if mode == "rename" else None,
        )
        if mode == "new":
            changed_plan = PlanService.create_plan(name)
            storage.save_plan(changed_plan)
            activate_plan(storage, settings, changed_plan)
            message = f"已新建并切换到方案“{name.strip()}”。"
        elif mode == "copy":
            changed_plan = PlanService.duplicate_plan(plan, name)
            storage.save_plan(changed_plan)
            activate_plan(storage, settings, changed_plan)
            message = f"已复制并切换到方案“{name.strip()}”。"
        else:
            changed_plan = PlanService.rename_plan(plan, name)
            storage.save_plan(changed_plan)
            set_active_plan(changed_plan)
            clear_plan_view_state()
            message = f"已重命名为“{name.strip()}”。"
    except (PlanServiceError, ValidationError, StorageError) as exc:
        st.error(f"操作失败：{exc}")
        return

    set_flash(message, "success")
    _close_plan_action()


def _render_plan_toolbar(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> PlanInventory:
    st.markdown("#### 多方案管理")
    inventory = storage.list_plans()
    plans = inventory.plans
    plan_ids = [item.id for item in plans]
    if plan.id not in plan_ids:
        plan_ids.insert(0, plan.id)
    by_id = {item.id: item for item in plans}
    by_id[plan.id] = plan

    pending_selector = st.session_state.pop(_PLAN_SELECTOR_PENDING, None)
    if pending_selector in plan_ids:
        st.session_state[_PLAN_SELECTOR] = pending_selector
    if (
        _PLAN_SELECTOR not in st.session_state
        or st.session_state[_PLAN_SELECTOR] not in plan_ids
    ):
        st.session_state[_PLAN_SELECTOR] = plan.id

    select_col, new_col, copy_col, rename_col = st.columns([3, 1, 1, 1])
    with select_col:
        selected_id = st.selectbox(
            "当前方案",
            options=plan_ids,
            format_func=lambda plan_id: _plan_label(by_id[plan_id]),
            key=_PLAN_SELECTOR,
        )
    with new_col:
        if st.button("新建方案", width="stretch"):
            _open_plan_action("new")
    with copy_col:
        if st.button("复制当前", width="stretch"):
            _open_plan_action("copy")
    with rename_col:
        if st.button("重命名", width="stretch"):
            _open_plan_action("rename")

    if inventory.errors:
        with st.expander("有方案文件未能加载", expanded=False):
            for error in inventory.errors:
                st.caption(error)

    if selected_id != plan.id:
        selected_plan = by_id.get(selected_id)
        if selected_plan is None:
            st.session_state[_PLAN_SELECTOR_PENDING] = plan.id
            set_flash("所选方案不存在或无法加载，已保留当前方案。", "error")
            st.rerun()
        else:
            try:
                activate_plan(storage, settings, selected_plan)
            except StorageError as exc:
                st.session_state[_PLAN_SELECTOR_PENDING] = plan.id
                set_flash(f"切换方案失败：{exc}", "error")
                st.rerun()
            else:
                set_flash(f"已切换到方案“{selected_plan.name}”。", "info")
                st.rerun()

    action = st.session_state.get(_PLAN_ACTION)
    if isinstance(action, dict):
        _plan_action_dialog(settings, plan, storage)
    return inventory


@st.dialog("清理请假冲突", width="large", on_dismiss=_dismiss_leave_cleanup)
def _leave_cleanup_dialog(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    conflicts = LeaveService.find_conflicts(settings, plan)
    if not conflicts:
        st.info("当前方案没有请假冲突。")
        if st.button("关闭", width="stretch"):
            _close_leave_cleanup()
        return

    people = {person.id: person.name for person in settings.people}
    shifts = {shift.id: shift.name for shift in settings.shifts}
    st.warning(
        f"当前方案有 {len(conflicts)} 条安排落在请假区间。"
        "清理只影响当前方案，其他方案中的冲突仍会在各自健康分中提示。"
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "日期": item.date.isoformat(),
                    "人员": people.get(item.person_id, item.person_id),
                    "班次": shifts.get(item.shift_id, item.shift_id),
                }
                for item in conflicts
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption("确认后会删除这些冲突班次；当天其他安排和交接备注保留，写入前自动备份。")

    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("确认移除", type="primary", width="stretch"):
        try:
            result = LeaveService.remove_conflicts(settings, plan)
            persist_active_plan(storage, result.plan)
        except StorageError as exc:
            st.error(f"清理失败：{exc}")
            return
        set_flash(
            f"已从当前方案移除 {len(result.removed)} 条请假冲突安排。",
            "success",
        )
        _close_leave_cleanup()
    if cancel_col.button("取消", width="stretch"):
        _close_leave_cleanup()


def _swap_signature(
    settings: AppSettings,
    plan: SchedulePlan,
    target_person_id: str,
) -> str:
    payload = "\n".join(
        [
            settings.model_dump_json(),
            plan.id,
            plan.updated_at.isoformat(),
            target_person_id,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_swap_section(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
    current_health: HealthResult,
    current_coverage: CoverageReport,
) -> None:
    st.divider()
    st.markdown("#### 调班推荐")
    loads = SwapService.weighted_loads(settings, plan)
    people = {person.id: person for person in settings.people}
    assigned_people = {
        assignment.person_id
        for day in plan.days
        for assignment in day.assignments
        if assignment.person_id in people and people[assignment.person_id].is_active
    }
    eligible = sorted(
        assigned_people,
        key=lambda person_id: (-loads.get(person_id, 0.0), people[person_id].name),
    )
    if len(eligible) < 2:
        st.caption("至少需要两名有排班且启用中的人员，才能生成两两换班推荐。")
        st.session_state.pop(_SWAP_RESULTS, None)
        return

    if (
        _SWAP_TARGET not in st.session_state
        or st.session_state[_SWAP_TARGET] not in eligible
    ):
        st.session_state[_SWAP_TARGET] = eligible[0]

    target_id = st.selectbox(
        "选择高负荷人员",
        options=eligible,
        format_func=lambda person_id: (
            f"{people[person_id].name} · 负荷 {loads.get(person_id, 0.0):.2f}"
        ),
        key=_SWAP_TARGET,
    )
    st.caption(
        "推荐只交换双方已有的启用班次；候选必须无硬约束、目标人员负荷下降，"
        "且健康分不低于当前。按负荷下降量优先排序。"
    )
    signature = _swap_signature(settings, plan, target_id)
    stored = st.session_state.get(_SWAP_RESULTS)
    if (
        isinstance(stored, dict)
        and stored.get("signature") != signature
    ):
        st.session_state.pop(_SWAP_RESULTS, None)
        stored = None

    if st.button("寻找 Top 3 调班方案", key="find_swap_recommendations"):
        try:
            recommendations = SwapService.recommend(
                settings,
                plan,
                target_id,
            )
        except SwapServiceError as exc:
            st.session_state.pop(_SWAP_RESULTS, None)
            st.error(f"无法生成推荐：{exc}")
            stored = None
        else:
            stored = {
                "signature": signature,
                "target_id": target_id,
                "items": recommendations,
            }
            st.session_state[_SWAP_RESULTS] = stored

    if not isinstance(stored, dict) or stored.get("signature") != signature:
        return

    recommendations = stored.get("items", ())
    if not recommendations:
        st.info(
            "没有找到满足条件的两两交换。通常需要目标人员承担更重班次，"
            "并由另一名人员承担较轻班次，同时不新增硬约束或拉低健康分。"
        )
        return

    shifts = {shift.id: shift.name for shift in settings.shifts}
    for index, recommendation in enumerate(recommendations, start=1):
        if not isinstance(recommendation, SwapRecommendation):
            continue
        candidate_coverage = CoverageService.build_report(
            settings, recommendation.candidate_plan
        )
        target_person = people.get(recommendation.target_person_id)
        partner_person = people.get(recommendation.partner_person_id)
        target_name = (
            target_person.name
            if target_person is not None
            else recommendation.target_person_id
        )
        partner_display = (
            partner_person.name
            if partner_person is not None
            else recommendation.partner_person_id
        )
        target_shift = shifts.get(
            recommendation.target_shift_id,
            recommendation.target_shift_id,
        )
        partner_shift = shifts.get(
            recommendation.partner_shift_id,
            recommendation.partner_shift_id,
        )
        health_delta = recommendation.health_delta
        health_text = (
            f"{recommendation.health_before} → {recommendation.health_after}"
            f"（{health_delta:+d}）"
        )

        with st.container(border=True):
            st.markdown(f"**方案 {index}** · 健康分 {health_text}")
            st.markdown(
                f"`{target_name}` {recommendation.target_date.isoformat()} "
                f"{target_shift} ↔ "
                f"`{partner_display}` {recommendation.partner_date.isoformat()} "
                f"{partner_shift}"
            )
            load_col, partner_col, health_col, coverage_col = st.columns(4)
            load_col.metric(
                "目标负荷",
                f"{recommendation.target_load_before:g} → "
                f"{recommendation.target_load_after:g}",
            )
            partner_col.metric(
                "同伴负荷",
                f"{recommendation.partner_load_before:g} → "
                f"{recommendation.partner_load_after:g}",
            )
            health_col.metric("健康分", health_text)
            coverage_col.metric(
                "缺岗班次",
                f"{len(current_coverage.shortages)} → "
                f"{len(candidate_coverage.shortages)}",
            )
            st.caption(
                f"应用前会再次校验当前方案指纹并自动备份。"
                f"候选硬约束：0。"
            )
            if st.button(
                "应用此交换",
                key=(
                    f"apply_swap::{index}::{recommendation.target_person_id}"
                    f"::{signature[:10]}"
                ),
                type="primary",
                width="stretch",
            ):
                if _swap_signature(settings, plan, target_id) != signature:
                    st.session_state.pop(_SWAP_RESULTS, None)
                    st.error("方案或配置已变化，请重新生成推荐。")
                    st.rerun()
                try:
                    persist_active_plan(storage, recommendation.candidate_plan)
                except StorageError as exc:
                    st.error(f"应用调班失败：{exc}")
                    return
                st.session_state.pop(_SWAP_RESULTS, None)
                set_flash(
                    f"已应用 {target_name} 与 {partner_display} 的换班方案，"
                    f"健康分 {recommendation.health_before} → "
                    f"{recommendation.health_after}。",
                    "success",
                )
                st.rerun()


def _repair_signature(
    settings: AppSettings,
    plan: SchedulePlan,
    violation_key: tuple,
) -> str:
    payload = "\n".join(
        [
            settings.model_dump_json(),
            plan.id,
            plan.updated_at.isoformat(),
            repr(violation_key),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _render_repair_preview(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
    current_coverage: CoverageReport,
) -> None:
    stored = st.session_state.get(_REPAIR_PREVIEW)
    if not isinstance(stored, dict):
        return

    violation_key = stored.get("violation_key")
    result = stored.get("result")
    current_keys = {
        violation.key
        for violation in ConstraintService.find_hard_violations(settings, plan)
    }
    if (
        violation_key not in current_keys
        or stored.get("signature")
        != _repair_signature(settings, plan, violation_key)
        or not isinstance(result, RepairSearchResult)
    ):
        st.session_state.pop(_REPAIR_PREVIEW, None)
        return

    st.markdown("#### 智能修复预览")
    recommendation = result.recommendation
    if recommendation is None:
        st.warning(f"本次尝试未找到可应用修复：{result.reason}")
        if st.button("关闭修复预览", key="close_empty_repair_preview"):
            st.session_state.pop(_REPAIR_PREVIEW, None)
            st.rerun()
        return

    people = {person.id: person.name for person in settings.people}
    shifts = {shift.id: shift.name for shift in settings.shifts}
    candidate_coverage = CoverageService.build_report(
        settings, recommendation.candidate_plan
    )
    operation_label = (
        "两两换班（优先）"
        if recommendation.operation == RepairService.SWAP
        else "空闲替补（兜底）"
    )

    with st.container(border=True):
        st.caption(
            f"修复策略：{operation_label} · 消除所选违规且不新增其他硬约束"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "操作": "新增" if change.action == "added" else "移除",
                        "日期": change.date.isoformat(),
                        "人员": people.get(change.person_id, change.person_id),
                        "班次": shifts.get(change.shift_id, change.shift_id),
                    }
                    for change in recommendation.changes
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        hard_col, health_col, gap_col, total_gap_col = st.columns(4)
        hard_col.metric(
            "硬约束",
            f"{recommendation.hard_before} → {recommendation.hard_after}",
        )
        health_col.metric(
            "健康分",
            f"{recommendation.health_before} → {recommendation.health_after}",
        )
        gap_col.metric(
            "缺岗班次",
            f"{len(current_coverage.shortages)} → "
            f"{len(candidate_coverage.shortages)}",
        )
        total_gap_col.metric(
            "总缺口",
            f"{current_coverage.total_gap} → {candidate_coverage.total_gap}",
        )
        if recommendation.hard_after:
            st.warning(
                f"修复后仍有 {recommendation.hard_after} 个硬约束；"
                "本次只保证消除所选违规且不新增冲突。"
            )
        st.caption(
            "下方软约束明细仍展示当前方案；应用修复后，健康分与明细会一起刷新。"
            "应用前会重新校验方案与配置指纹，并自动备份原文件。"
        )

        apply_col, cancel_col = st.columns(2)
        if apply_col.button(
            "应用修复",
            key=(
                "apply_repair::"
                + "::".join(str(part) for part in violation_key)
            ),
            type="primary",
            width="stretch",
        ):
            if (
                _repair_signature(settings, plan, violation_key)
                != stored.get("signature")
            ):
                st.session_state.pop(_REPAIR_PREVIEW, None)
                st.error("方案或配置已变化，请重新尝试修复。")
                st.rerun()
            try:
                persist_active_plan(storage, recommendation.candidate_plan)
            except StorageError as exc:
                st.error(f"应用修复失败：{exc}")
                return
            st.session_state.pop(_REPAIR_PREVIEW, None)
            set_flash(
                "已消除所选硬约束"
                + (
                    f"；方案仍剩 {recommendation.hard_after} 个硬约束。"
                    if recommendation.hard_after
                    else "，方案已无硬约束。"
                ),
                "success" if not recommendation.hard_after else "warning",
            )
            st.rerun()
        if cancel_col.button("取消修复", width="stretch"):
            st.session_state.pop(_REPAIR_PREVIEW, None)
            st.rerun()


def _score_text(health: HealthResult) -> str:
    return "—" if health.score is None else str(health.score)


def _rate_text(health: HealthResult) -> str:
    rate = health.soft.preference_rate
    return "—" if rate is None else f"{rate:.0%}"


def _integer_delta(current: int | None, reference: int | None) -> str:
    if current is None or reference is None:
        return "—"
    delta = current - reference
    return f"{delta:+d}" if delta else "0"


def _rate_delta(current: HealthResult, reference: HealthResult) -> str:
    current_rate = current.soft.preference_rate
    reference_rate = reference.soft.preference_rate
    if current_rate is None or reference_rate is None:
        return "—"
    delta = (current_rate - reference_rate) * 100
    return f"{delta:+.0f}pp" if abs(delta) >= 0.5 else "0pp"


def _render_comparison(
    settings: AppSettings,
    plan: SchedulePlan,
    inventory: PlanInventory,
    current_health: HealthResult,
    current_coverage: CoverageReport,
) -> None:
    st.divider()
    st.markdown("#### 方案差异对比")
    candidates = [item for item in inventory.plans if item.id != plan.id]
    if not candidates:
        st.info("至少需要两个方案才能对比。可使用上方“新建方案”或“复制当前”。")
        return

    candidate_ids = [item.id for item in candidates]
    if (
        _COMPARE_REFERENCE not in st.session_state
        or st.session_state[_COMPARE_REFERENCE] not in candidate_ids
    ):
        st.session_state[_COMPARE_REFERENCE] = candidate_ids[0]

    candidates_by_id = {item.id: item for item in candidates}
    reference_id = st.selectbox(
        "参考方案",
        options=candidate_ids,
        format_func=lambda plan_id: _plan_label(candidates_by_id[plan_id]),
        key=_COMPARE_REFERENCE,
    )
    reference = candidates_by_id[reference_id]
    reference_health = ScoringService.evaluate(settings, reference)
    reference_coverage = CoverageService.build_report(settings, reference)
    comparison = ComparisonService.compare(plan, reference)

    st.caption(
        f"当前方案「{plan.name}」 vs 参考方案「{reference.name}」；"
        "差异方向为“当前方案相对参考方案”。"
    )

    summary_rows = [
        {
            "指标": "健康分",
            "当前方案": _score_text(current_health),
            "参考方案": _score_text(reference_health),
            "变化（当前 - 参考）": _integer_delta(
                current_health.score, reference_health.score
            ),
        },
        {
            "指标": "状态",
            "当前方案": current_health.status,
            "参考方案": reference_health.status,
            "变化（当前 - 参考）": "—",
        },
        {
            "指标": "硬约束违规",
            "当前方案": len(current_health.hard_violations),
            "参考方案": len(reference_health.hard_violations),
            "变化（当前 - 参考）": _integer_delta(
                len(current_health.hard_violations),
                len(reference_health.hard_violations),
            ),
        },
        {
            "指标": "缺岗班次",
            "当前方案": len(current_coverage.shortages),
            "参考方案": len(reference_coverage.shortages),
            "变化（当前 - 参考）": _integer_delta(
                len(current_coverage.shortages),
                len(reference_coverage.shortages),
            ),
        },
        {
            "指标": "总缺口人数",
            "当前方案": current_coverage.total_gap,
            "参考方案": reference_coverage.total_gap,
            "变化（当前 - 参考）": _integer_delta(
                current_coverage.total_gap,
                reference_coverage.total_gap,
            ),
        },
        {
            "指标": "偏好满足率",
            "当前方案": _rate_text(current_health),
            "参考方案": _rate_text(reference_health),
            "变化（当前 - 参考）": _rate_delta(
                current_health, reference_health
            ),
        },
    ]
    st.dataframe(
        pd.DataFrame(summary_rows),
        width="stretch",
        hide_index=True,
    )

    added_col, removed_col, changed_col, note_col = st.columns(4)
    added_col.metric("新增", comparison.added_count)
    removed_col.metric("删除", comparison.removed_count)
    changed_col.metric("变更", comparison.changed_count)
    note_col.metric("备注变化", comparison.note_change_count)

    if not comparison.has_changes:
        st.success("两个方案的排班和交接备注完全一致。")
        return

    people = {person.id: person.name for person in settings.people}
    shifts = {shift.id: shift.name for shift in settings.shifts}
    type_labels = {
        ComparisonService.ADDED: "＋ 新增",
        ComparisonService.REMOVED: "－ 删除",
        ComparisonService.CHANGED: "↔ 变更",
    }

    def shift_name(shift_id: str | None) -> str:
        if shift_id is None:
            return "—"
        return shifts.get(shift_id, f"未知班次({shift_id})")

    if comparison.assignment_diffs:
        diff_rows = [
            {
                "日期": item.date.isoformat(),
                "人员": people.get(item.person_id, item.person_id),
                "参考方案": shift_name(item.reference_shift_id),
                "当前方案": shift_name(item.current_shift_id),
                "类型": type_labels.get(item.change_type, item.change_type),
            }
            for item in sorted(
                comparison.assignment_diffs,
                key=lambda item: (
                    item.date,
                    people.get(item.person_id, item.person_id),
                ),
            )
        ]
        st.caption("仅展示有变化的记录；未变化排班不重复列出。")
        st.dataframe(
            pd.DataFrame(diff_rows),
            width="stretch",
            hide_index=True,
        )

    if comparison.note_diffs:
        with st.expander(
            f"交接备注差异（{comparison.note_change_count}）",
            expanded=False,
        ):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "日期": item.date.isoformat(),
                            "参考方案备注": item.reference_note or "—",
                            "当前方案备注": item.current_note or "—",
                        }
                        for item in comparison.note_diffs
                    ]
                ),
                width="stretch",
                hide_index=True,
            )


def render(
    settings: AppSettings,
    plan: SchedulePlan,
    storage: JsonStorage,
) -> None:
    inventory = _render_plan_toolbar(settings, plan, storage)
    st.divider()

    health = ScoringService.evaluate(settings, plan)
    coverage = CoverageService.build_report(settings, plan)
    leave_conflicts = LeaveService.find_conflicts(settings, plan)
    preference_rate = health.soft.preference_rate

    st.subheader("方案健康度")
    score_col, status_col, violation_col, preference_col = st.columns(4)
    score_col.metric(
        "健康分",
        "—" if health.score is None else health.score,
        help="满分 100；任一硬约束违规时总分记 0（一票否决）",
    )
    status_col.metric("状态", health.status)
    violation_col.metric("硬约束违规", len(health.hard_violations))
    preference_col.metric(
        "偏好满足率",
        "—" if preference_rate is None else f"{preference_rate:.0%}",
    )

    if health.hard_violations:
        st.error(
            f"检测到 {len(health.hard_violations)} 个硬约束违规，"
            "健康分按一票否决规则记为 0。"
        )
        with st.expander("查看硬约束违规明细", expanded=True):
            for violation in health.hard_violations:
                message_col, action_col = st.columns([5, 1.5])
                message_col.markdown(f"- {violation.message}")
                if violation.code == ConstraintService.NIGHT_THEN_EARLY:
                    button_key = (
                        "try_repair::"
                        + "::".join(str(part) for part in violation.key)
                    )
                    if action_col.button(
                        "尝试智能修复",
                        key=button_key,
                        width="stretch",
                    ):
                        result = RepairService.propose(
                            settings,
                            plan,
                            violation,
                        )
                        st.session_state[_REPAIR_PREVIEW] = {
                            "signature": _repair_signature(
                                settings, plan, violation.key
                            ),
                            "violation_key": violation.key,
                            "result": result,
                        }
                else:
                    action_col.caption("请假冲突")

        if leave_conflicts:
            st.warning(
                f"当前方案有 {len(leave_conflicts)} 条安排与请假冲突，"
                "可以预览影响后从当前方案移除。"
            )
            if st.button(
                f"预览并清理请假冲突（{len(leave_conflicts)}）",
                key="preview_leave_cleanup",
            ):
                st.session_state[_LEAVE_CLEANUP] = True
                st.rerun()

        _render_repair_preview(
            settings,
            plan,
            storage,
            coverage,
        )
    else:
        st.session_state.pop(_REPAIR_PREVIEW, None)
        if health.score is None:
            st.info("方案还没有排班数据，健康分只会在有排班后计算。")
        elif health.status == ScoringService.STATUS_HEALTHY:
            st.success("当前方案健康，未发现硬约束违规。")
        else:
            st.warning("当前方案没有硬约束违规，但软约束仍有优化空间。")

    st.markdown("#### 软约束明细")
    if health.has_data:
        soft = health.soft
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "项目": "连续同班次",
                        "扣分": soft.continuity_penalty,
                        "说明": (
                            "连续同班次超过 2 天的额外天数："
                            f"{soft.continuity_extra_days}"
                        ),
                    },
                    {
                        "项目": "负荷均衡",
                        "扣分": soft.load_penalty,
                        "说明": f"有排班人员的加权负荷极差：{soft.load_spread:g}",
                    },
                    {
                        "项目": "偏好满足",
                        "扣分": soft.preference_penalty,
                        "说明": (
                            "无可用偏好数据"
                            if soft.preference_total == 0
                            else (
                                f"满足 {soft.preference_satisfied} / "
                                f"{soft.preference_total} 次"
                            )
                        ),
                    },
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            f"软约束合计扣 {soft.total_penalty} 分；"
            "健康分为 100 分起扣，硬约束优先否决。"
        )
    else:
        st.caption("暂无排班数据，软约束不评分。")

    st.markdown("#### 缺岗预警")
    if not coverage.checked_dates:
        st.caption("方案还没有已有安排或备注的日期，暂不计算缺岗。")
    elif coverage.has_shortages:
        st.warning(
            f"{len(coverage.shortages)} 个班次低于最低在岗人数，"
            f"总缺口 {coverage.total_gap} 人；检查日期 "
            f"{coverage.checked_dates[0].isoformat()} 至 "
            f"{coverage.checked_dates[-1].isoformat()}。"
        )
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
    else:
        st.success(
            f"已检查 {len(coverage.checked_dates)} 个已有安排的日期，"
            "所有启用班次均达到最低人数。"
        )

    _render_swap_section(settings, plan, storage, health, coverage)
    _render_comparison(settings, plan, inventory, health, coverage)
    st.caption(f"当前方案：{plan.name}")

    if st.session_state.get(_LEAVE_CLEANUP):
        _leave_cleanup_dialog(settings, plan, storage)
