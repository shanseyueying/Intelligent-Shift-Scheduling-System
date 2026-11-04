"""Tab 3：加权负荷与偏好/请假统计看板。"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from src.models import AppSettings, SchedulePlan
from src.services.coverage_service import CoverageService
from src.services.stats_service import PersonStats, StatsService

# 已通过 dataviz 校验的高对比度分类色板（浅色图表表面）。
_CATEGORICAL_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
]
_OTHER_COLOR = "#898781"
_SURFACE = "#F8FAFC"
_TEXT_SECONDARY = "#52514E"
_MUTED = "#898781"
_GRID = "#E1E0D9"


def _other_label(settings: AppSettings) -> str:
    label = "其他"
    existing = {shift.name for shift in settings.shifts}
    while label in existing:
        label += "·"
    return label


def _category_mapping(settings: AppSettings) -> tuple[dict[str, tuple[str, str, int]], str]:
    other = _other_label(settings)
    by_shift: dict[str, tuple[str, str, int]] = {}
    for index, shift in enumerate(settings.shifts):
        if index < len(_CATEGORICAL_COLORS):
            by_shift[shift.id] = (
                shift.name,
                _CATEGORICAL_COLORS[index],
                index,
            )
        else:
            by_shift[shift.id] = (other, _OTHER_COLOR, len(_CATEGORICAL_COLORS))
    return by_shift, other


def _build_chart_data(
    settings: AppSettings,
    stats,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    list[str],
    list[str],
    list[str],
    float,
]:
    by_shift, other = _category_mapping(settings)
    rows: list[dict[str, object]] = []
    for person in stats.people:
        aggregated: dict[str, dict[str, object]] = {}
        for shift_load in person.shift_loads:
            category, color, order = by_shift.get(
                shift_load.shift_id,
                (other, _OTHER_COLOR, len(_CATEGORICAL_COLORS)),
            )
            if shift_load.weighted_load <= 0:
                continue
            entry = aggregated.setdefault(
                category,
                {
                    "person_name": person.name,
                    "shift_category": category,
                    "color": color,
                    "category_order": order,
                    "weighted_load": 0.0,
                    "assignment_count": 0,
                },
            )
            entry["weighted_load"] = (
                float(entry["weighted_load"]) + shift_load.weighted_load
            )
            entry["assignment_count"] = (
                int(entry["assignment_count"]) + shift_load.assignment_count
            )
        rows.extend(aggregated.values())

    people_order = [person.name for person in stats.people]
    categories = sorted(
        {
            (str(row["category_order"]), str(row["shift_category"]))
            for row in rows
        }
    )
    category_order = [category for _, category in categories]
    color_by_category = {
        str(row["shift_category"]): str(row["color"]) for row in rows
    }
    color_range = [
        color_by_category[category] for category in category_order
    ]

    totals = pd.DataFrame(
        [
            {
                "person_name": person.name,
                "weighted_load": person.weighted_load,
                "load_label": f"{person.weighted_load:g}",
            }
            for person in stats.people
        ]
    )
    max_load = max((person.weighted_load for person in stats.people), default=0.0)
    x_max = max_load * 1.18 if max_load > 0 else 1.0
    return (
        pd.DataFrame(rows),
        totals,
        people_order,
        category_order,
        color_range,
        x_max,
    )


def _build_chart(
    chart_rows: pd.DataFrame,
    totals: pd.DataFrame,
    people_order: list[str],
    category_order: list[str],
    color_range: list[str],
    x_max: float,
) -> alt.LayerChart:
    y_encoding = alt.Y(
        "person_name:N",
        sort=people_order,
        title=None,
        axis=alt.Axis(
            labelColor=_TEXT_SECONDARY,
            labelFontSize=12,
            labelLimit=160,
            domain=False,
            ticks=False,
        ),
    )
    x_encoding = alt.X(
        "weighted_load:Q",
        title="加权负荷",
        scale=alt.Scale(domain=[0, x_max], nice=False),
        axis=alt.Axis(
            grid=True,
            gridColor=_GRID,
            gridWidth=1,
            domain=False,
            ticks=False,
            tickCount=6,
            labelColor=_MUTED,
            labelFontSize=10,
            titleColor=_MUTED,
            titleFontSize=11,
        ),
    )
    legend = (
        alt.Legend(
            orient="top",
            direction="horizontal",
            title=None,
            labelColor=_TEXT_SECONDARY,
            labelFontSize=11,
            symbolType="square",
            symbolSize=90,
        )
        if len(category_order) > 1
        else None
    )
    color_encoding = alt.Color(
        "shift_category:N",
        title=None,
        scale=alt.Scale(domain=category_order, range=color_range),
        legend=legend,
    )

    bars = (
        alt.Chart(chart_rows)
        .mark_bar(
            size=18,
            cornerRadiusEnd=4,
            stroke=_SURFACE,
            strokeWidth=2,
        )
        .encode(
            x=x_encoding,
            y=y_encoding,
            color=color_encoding,
            order=alt.Order("category_order:Q"),
            tooltip=[
                alt.Tooltip("person_name:N", title="人员"),
                alt.Tooltip("shift_category:N", title="班次"),
                alt.Tooltip("weighted_load:Q", title="加权负荷", format=".2f"),
                alt.Tooltip("assignment_count:Q", title="排班次数", format="d"),
            ],
        )
    )
    labels = (
        alt.Chart(totals)
        .mark_text(
            align="left",
            baseline="middle",
            dx=5,
            color=_TEXT_SECONDARY,
            fontSize=11,
        )
        .encode(
            x=alt.X(
                "weighted_load:Q",
                scale=alt.Scale(domain=[0, x_max], nice=False),
            ),
            y=alt.Y("person_name:N", sort=people_order),
            text=alt.Text("load_label:N"),
        )
    )
    height = max(220, min(620, 72 + 44 * len(people_order)))
    return (bars + labels).properties(
        height=height,
        background=_SURFACE,
    ).configure_view(stroke=None)


def _preference_text(person: PersonStats) -> str:
    if person.preference_total:
        return (
            f"{person.preference_satisfied}/{person.preference_total}"
            f" ({person.preference_rate:.0%})"
        )
    if person.assignment_count == 0:
        return "未排班"
    return "无偏好"


def _composition_text(person: PersonStats) -> str:
    if not person.shift_loads:
        return "—"
    return " · ".join(
        f"{shift.shift_name} × {shift.assignment_count}"
        for shift in person.shift_loads
    )


def render(settings: AppSettings, plan: SchedulePlan) -> None:
    stats = StatsService.build(settings, plan)
    coverage = CoverageService.build_report(settings, plan)

    st.subheader("人员负荷与偏好")
    st.caption(
        "统计包含全部启用人员（含零排班）；健康分中的负荷极差只统计有排班人员。"
        "图表使用固定的高对比度分类色板，班次颜色仍以日历/配置页为准。"
    )

    if not stats.people:
        st.info("当前没有启用人员，请先在基础配置中启用人员。")
        return

    preference_rate = stats.preference_rate
    load_col, average_col, preference_col, gap_col = st.columns(4)
    load_col.metric(
        "总加权负荷",
        f"{stats.total_weighted_load:g}",
        help="所有启用人员按班次负荷权重累计的负荷分。",
    )
    average_col.metric(
        "人均负荷",
        f"{stats.average_weighted_load:g}",
        help="总加权负荷 / 启用人员数（包含零排班人员）。",
    )
    preference_col.metric(
        "偏好满足率",
        "—" if preference_rate is None else f"{preference_rate:.0%}",
    )
    gap_col.metric(
        "缺岗总人数",
        coverage.total_gap,
        help="方案已有安排日期中，各启用班次低于最低在岗人数的缺口总和；详情见 Tab 2。",
    )
    st.caption(
        f"{stats.active_people_count} 名启用人员 · "
        f"{stats.total_assignments} 条排班 · "
        f"加权负荷极差 {stats.load_spread:g}"
    )

    st.markdown("#### 加权负荷分布")
    if stats.total_assignments == 0:
        st.info("当前方案还没有排班数据，负荷图将在完成排班后显示。")
    else:
        (
            chart_rows,
            totals,
            people_order,
            category_order,
            color_range,
            x_max,
        ) = _build_chart_data(settings, stats)
        if chart_rows.empty:
            st.info("当前班次缺少负荷权重，暂无可视化数据。")
        else:
            chart = _build_chart(
                chart_rows,
                totals,
                people_order,
                category_order,
                color_range,
                x_max,
            )
            st.altair_chart(
                chart,
                width="stretch",
                theme="streamlit",
                alt=(
                    "按人员展示的加权负荷堆叠条形图，颜色代表班次；"
                    "具体数值见下方人员明细表。"
                ),
            )

    st.markdown("#### 人员明细")
    detail_rows = [
        {
            "人员": person.name,
            "排班次数": person.assignment_count,
            "加权负荷": person.weighted_load,
            "偏好满足": _preference_text(person),
            "请假天数": person.leave_days,
            "班次构成": _composition_text(person),
        }
        for person in stats.people
    ]
    st.dataframe(
        pd.DataFrame(detail_rows),
        width="stretch",
        hide_index=True,
        column_config={
            "加权负荷": st.column_config.NumberColumn(
                "加权负荷",
                format="%.2f",
            ),
        },
    )
    st.caption(f"当前方案：{plan.name}。请假天数按方案最早至最晚日期裁切，重叠区间只计一次。")
