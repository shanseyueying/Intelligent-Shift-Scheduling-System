"""智能排班辅助系统入口。

运行：streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from src.models import AppSettings, SchedulePlan
from src.ui.state import (
    activate_plan,
    clear_session_cache,
    consume_flash,
    get_active_plan,
    get_fatal_error,
    get_notices,
    get_settings,
    init_app_state,
    set_flash,
)
from src.ui.tabs import calendar_tab, compare_tab, config_tab, stats_tab
from src.utils.storage import JsonStorage, StorageError

_PAGE_TITLE = "智能排班辅助系统"
_TAB_LABELS = [
    "📅 排班日历",
    "⚖️ 方案对比与打分",
    "📊 数据统计",
    "⚙️ 基础配置",
]
_SWITCH_PLAN_DIALOG = "sidebar.switch_plan_dialog"


def _plan_option_label(plan: SchedulePlan) -> str:
    local_time = plan.updated_at.astimezone()
    return f"{plan.name} · {local_time.strftime('%m-%d %H:%M')}"


def _dismiss_switch_plan_dialog() -> None:
    st.session_state.pop(_SWITCH_PLAN_DIALOG, None)


def _close_switch_plan_dialog() -> None:
    st.session_state.pop(_SWITCH_PLAN_DIALOG, None)
    st.rerun()


@st.dialog("切换方案", on_dismiss=_dismiss_switch_plan_dialog)
def _switch_plan_dialog(
    storage: JsonStorage,
    settings: AppSettings,
    plan: SchedulePlan,
) -> None:
    inventory = storage.list_plans()
    plans = list(inventory.plans)
    if not any(item.id == plan.id for item in plans):
        plans.insert(0, plan)
    if not plans:
        st.info("当前没有可切换的方案。")
        if st.button("关闭", width="stretch"):
            _close_switch_plan_dialog()
        return

    by_id = {item.id: item for item in plans}
    plan_ids = [item.id for item in plans]
    default_index = plan_ids.index(plan.id) if plan.id in plan_ids else 0
    selected_id = st.selectbox(
        "目标方案",
        options=plan_ids,
        index=default_index,
        format_func=lambda plan_id: _plan_option_label(by_id[plan_id]),
        key="sidebar.switch_plan.target",
    )
    if inventory.errors:
        with st.expander("有方案文件未能加载", expanded=False):
            for error in inventory.errors:
                st.caption(error)
    if selected_id == plan.id:
        st.caption("当前已经是这个方案。")

    switch_col, cancel_col = st.columns(2)
    if switch_col.button(
        "切换到此方案",
        type="primary",
        disabled=selected_id == plan.id,
        width="stretch",
    ):
        selected_plan = by_id[selected_id]
        try:
            activate_plan(storage, settings, selected_plan)
        except StorageError as exc:
            st.error(f"切换方案失败：{exc}")
            return
        set_flash(f"已切换到方案“{selected_plan.name}”。", "info")
        _close_switch_plan_dialog()
    if cancel_col.button("取消", width="stretch"):
        _close_switch_plan_dialog()


def _render_fatal_error(storage: JsonStorage, message: str) -> None:
    st.title(f"🗓️ {_PAGE_TITLE}")
    st.error("本地数据无法安全加载，系统已停止初始化，未覆盖任何文件。")
    st.code(message, language=None)
    st.markdown(
        f"""
**数据目录**：`{storage.data_dir}`

排查建议：

1. 检查 `data/settings.json` 或对应方案文件是否为合法 JSON；
2. 在 `data/backups/` 中查找同一文件最近一次自动备份；
3. 修复后点击下方“重新加载”，或重启 Streamlit。
"""
    )
    if st.button("修复后重新加载", type="primary", width="stretch"):
        clear_session_cache()
        st.rerun()
    st.stop()


def _render_sidebar(
    storage: JsonStorage,
    settings: AppSettings,
    plan: SchedulePlan,
) -> None:
    active_people_count = sum(
        1 for person in settings.people if person.is_active
    )
    total_people_count = len(settings.people)
    people_label = f"{active_people_count} 名启用人员"
    if total_people_count != active_people_count:
        people_label += f"（共 {total_people_count} 人）"

    with st.sidebar:
        st.markdown(
            """
            <div style="
                padding: 2px 0 12px 12px;
                border-left: 3px solid #4f46e5;
                border-bottom: 1px solid rgba(148, 163, 184, 0.28);
                cursor: default;
                user-select: none;
            ">
              <div style="
                  font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                  font-size: 1.42rem;
                  font-weight: 800;
                  letter-spacing: 0.14em;
                  line-height: 1.25;
                  background: linear-gradient(135deg, #1e3a8a, #6d28d9);
                  -webkit-background-clip: text;
                  background-clip: text;
                  color: transparent;
              ">智能排班</div>
              <div style="
                  margin-top: 4px;
                  color: #64748b;
                  font-size: 0.68rem;
                  letter-spacing: 0.16em;
              ">SCHEDULING ASSISTANT</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()
        st.caption("当前方案")
        st.markdown(f"**{plan.name}**")
        if st.button(
            "切换方案",
            key="sidebar.switch_plan.open",
            width="stretch",
        ):
            st.session_state[_SWITCH_PLAN_DIALOG] = True
            st.rerun()
        st.caption(people_label)
        st.divider()
        with st.expander("数据与存储", expanded=False):
            st.caption(str(storage.data_dir))
            st.caption("修改会自动做覆盖前备份，数据文件不进入 Git。")

    if st.session_state.get(_SWITCH_PLAN_DIALOG):
        _switch_plan_dialog(storage, settings, plan)


def main() -> None:
    st.set_page_config(
        page_title=_PAGE_TITLE,
        page_icon="🗓️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    storage = JsonStorage()
    init_app_state(storage)

    fatal_error = get_fatal_error()
    if fatal_error:
        _render_fatal_error(storage, fatal_error)

    settings = get_settings()
    plan = get_active_plan()
    _render_sidebar(storage, settings, plan)

    st.title(f"🗓️ {_PAGE_TITLE}")
    st.caption(
        "把周期排班变成可校验、可评分、可比较的工作流。"
        "点击日期编辑当天排班，或用周期模板批量铺排。"
    )
    for notice in get_notices():
        st.info(notice)

    flash = consume_flash()
    if flash:
        level, message = flash
        {
            "success": st.success,
            "info": st.info,
            "warning": st.warning,
            "error": st.error,
        }.get(level, st.info)(message)

    calendar_view, compare_view, stats_view, config_view = st.tabs(
        _TAB_LABELS,
        default=_TAB_LABELS[0],
        key="nav.active_tab",
        on_change="rerun",
    )
    active_tab = st.session_state.get("nav.active_tab", _TAB_LABELS[0])
    with calendar_view:
        calendar_tab.render(
            settings,
            plan,
            storage,
            is_active=active_tab == _TAB_LABELS[0],
        )
    with compare_view:
        compare_tab.render(settings, plan, storage)
    with stats_view:
        stats_tab.render(settings, plan)
    with config_view:
        config_tab.render(settings, plan, storage)


if __name__ == "__main__":
    main()
