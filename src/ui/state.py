"""Streamlit 会话状态的唯一初始化入口。

所有页面都从 session_state 取当前配置和方案，避免按钮触发 rerun 后
重新构造对象、丢失尚未持久化的编辑结果。
"""

from __future__ import annotations

import streamlit as st

from src.models import AppSettings, SchedulePlan
from src.services.demo_service import build_demo_workspace
from src.services.plan_service import PlanService
from src.utils.storage import JsonStorage, StorageError

_SETTINGS = "app_settings"
_ACTIVE_PLAN = "active_plan"
_FATAL_ERROR = "fatal_data_error"
_NOTICES = "app_notices"
_FLASH = "app_flash"
_INITIALIZED = "_app_state_initialized"
_PLAN_SELECTOR_PENDING = "plan.selector.pending"
_CALENDAR_VERSION = "calendar.version"


def init_app_state(storage: JsonStorage) -> None:
    """首次 rerun 时加载磁盘数据；同一会话内后续 rerun 不重复读取。"""
    if st.session_state.get(_INITIALIZED):
        return

    notices: list[str] = []
    try:
        settings = storage.load_settings()
        if settings is None:
            inventory = storage.list_plans()
            if inventory.has_any_file:
                raise StorageError(
                    "未找到 settings.json，但 data/plans 中已有方案文件。"
                    "为避免覆盖用户数据，系统已停止自动初始化；"
                    "请从 data/backups 恢复设置文件后重新启动。"
                )
            settings, plan = build_demo_workspace()
            # 先写方案再写设置：即使中途失败，也不会出现“指向不存在方案”的配置。
            storage.save_plan(plan)
            storage.save_settings(settings)
            notices.append(
                "已创建示例人员、班次和空白方案；"
                "可在基础配置中调整，或直接点击日历开始排班。"
            )
        else:
            settings, plan, selection_notices = _resolve_active_plan(storage, settings)
            notices.extend(selection_notices)

        st.session_state[_SETTINGS] = settings
        st.session_state[_ACTIVE_PLAN] = plan
        st.session_state[_NOTICES] = notices
    except StorageError as exc:
        st.session_state[_FATAL_ERROR] = str(exc)
    finally:
        st.session_state[_INITIALIZED] = True


def _resolve_active_plan(
    storage: JsonStorage,
    settings: AppSettings,
) -> tuple[AppSettings, SchedulePlan, list[str]]:
    notices: list[str] = []
    inventory = storage.list_plans()
    if inventory.errors:
        notices.extend(f"跳过损坏的方案文件：{error}" for error in inventory.errors)

    active_id = settings.active_plan_id
    if active_id:
        matched = next(
            (plan for plan in inventory.plans if plan.id == active_id),
            None,
        )
        if matched is not None:
            return settings, matched, notices
        notices.append("原活动方案不可用，已切换到最近更新的可用方案。")

    if inventory.plans:
        plan = inventory.plans[0]
        updated_settings = _set_active_plan_id(settings, plan.id)
        storage.save_settings(updated_settings)
        return updated_settings, plan, notices

    # 所有方案都损坏时仍允许新建空白方案；损坏文件保持原样，不删除。
    plan = PlanService.create_plan("默认排班")
    storage.save_plan(plan)
    updated_settings = _set_active_plan_id(settings, plan.id)
    storage.save_settings(updated_settings)
    notices.append("没有可读的排班方案，已新建空白方案；原文件未被删除。")
    return updated_settings, plan, notices


def _set_active_plan_id(settings: AppSettings, plan_id: str) -> AppSettings:
    payload = settings.model_dump()
    payload["active_plan_id"] = plan_id
    return AppSettings.model_validate(payload)


def get_settings() -> AppSettings:
    settings = st.session_state.get(_SETTINGS)
    if settings is None:
        raise RuntimeError("应用状态尚未初始化")
    return settings


def get_active_plan() -> SchedulePlan:
    plan = st.session_state.get(_ACTIVE_PLAN)
    if plan is None:
        raise RuntimeError("应用状态尚未初始化")
    return plan


def set_active_plan(plan: SchedulePlan) -> None:
    """后续方案编辑/切换统一走此入口，确保 rerun 后取到最新对象。"""
    st.session_state[_ACTIVE_PLAN] = plan


def persist_active_plan(storage: JsonStorage, plan: SchedulePlan) -> None:
    """先落盘再更新会话；写盘失败时保留旧的内存状态。"""
    storage.save_plan(plan)
    st.session_state[_ACTIVE_PLAN] = plan


def persist_settings(storage: JsonStorage, settings: AppSettings) -> None:
    """配置更新同样先落盘，成功后才替换会话状态。"""
    storage.save_settings(settings)
    st.session_state[_SETTINGS] = settings


def activate_plan(
    storage: JsonStorage,
    settings: AppSettings,
    plan: SchedulePlan,
) -> None:
    """保存活动方案指针并切换会话，供任何 Tab 复用。

    先写 settings.json，成功后才替换内存状态，避免界面显示一个尚未落盘的
    活动方案。方案文件本身必须由调用方先保存；本函数不写排班内容。
    """
    updated_settings = _set_active_plan_id(settings, plan.id)
    persist_settings(storage, updated_settings)
    set_active_plan(plan)
    clear_plan_view_state()
    # Tab 2 的方案选择框可能在本轮已经渲染，延迟到下一轮再同步它的 session key。
    st.session_state[_PLAN_SELECTOR_PENDING] = plan.id


def clear_plan_view_state() -> None:
    """切换方案时清理与旧方案绑定的临时 UI 状态。"""
    # 只清理普通状态，不碰本轮已经实例化的 selectbox 等 widget key。
    # swap.target 会在下一轮由 Tab 2 校验并按需重置。
    for key in (
        "calendar.selected_date",
        "template.preview",
        "swap.results",
        "repair.preview",
        "plan.leave_cleanup",
    ):
        st.session_state.pop(key, None)
    # 重挂日历，防止 FullCalendar 上一条点击回调在切换方案后再次打开旧弹窗。
    if _CALENDAR_VERSION in st.session_state:
        st.session_state[_CALENDAR_VERSION] = (
            int(st.session_state[_CALENDAR_VERSION]) + 1
        )


def set_flash(message: str, level: str = "success") -> None:
    """跨 rerun 的一次性提示。"""
    st.session_state[_FLASH] = (level, message)


def consume_flash() -> tuple[str, str] | None:
    value = st.session_state.pop(_FLASH, None)
    if not value:
        return None
    return value


def get_fatal_error() -> str | None:
    return st.session_state.get(_FATAL_ERROR)


def get_notices() -> list[str]:
    return list(st.session_state.get(_NOTICES, []))


def clear_session_cache() -> None:
    """不触碰磁盘文件，仅允许用户在修复外部数据后重新加载。"""
    for key in (
        _SETTINGS,
        _ACTIVE_PLAN,
        _FATAL_ERROR,
        _NOTICES,
        _FLASH,
        _INITIALIZED,
    ):
        st.session_state.pop(key, None)
