"""智能排班辅助系统入口。

运行：streamlit run app.py
"""

from __future__ import annotations

import streamlit as st

from src.ui.state import (
    clear_session_cache,
    consume_flash,
    get_active_plan,
    get_fatal_error,
    get_notices,
    get_settings,
    init_app_state,
)
from src.ui.tabs import calendar_tab, compare_tab, config_tab, stats_tab
from src.utils.storage import JsonStorage

_PAGE_TITLE = "智能排班辅助系统"
_TAB_LABELS = [
    "📅 排班日历",
    "⚖️ 方案对比与打分",
    "📊 数据统计",
    "⚙️ 基础配置",
]


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
    plan_name: str,
    active_people_count: int,
    total_people_count: int,
) -> None:
    with st.sidebar:
        st.markdown("## 智能排班")
        st.caption("Scheduling Assistant")
        st.divider()
        st.markdown(f"**当前方案**  \n{plan_name}")
        people_label = f"{active_people_count} 名启用人员"
        if total_people_count != active_people_count:
            people_label += f"（共 {total_people_count} 人）"
        st.caption(people_label)
        st.divider()
        st.caption("本地数据目录")
        st.code(str(storage.data_dir), language=None)
        st.caption("修改会自动做覆盖前备份，数据文件不进入 Git。")


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

    active_people_count = sum(1 for person in settings.people if person.is_active)
    _render_sidebar(
        storage,
        plan.name,
        active_people_count,
        len(settings.people),
    )

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
    with calendar_view:
        calendar_tab.render(settings, plan, storage)
    with compare_view:
        compare_tab.render(settings, plan, storage)
    with stats_view:
        stats_tab.render(settings, plan)
    with config_view:
        config_tab.render(settings, plan, storage)


if __name__ == "__main__":
    main()
