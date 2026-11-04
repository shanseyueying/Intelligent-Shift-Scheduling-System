"""Streamlit 级冒烟测试，使用临时数据目录，避免接触真实用户数据。"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.services.config_service import ConfigService
from src.services.plan_service import PlanService
from src.services.schedule_service import ScheduleService
from src.utils.dates import local_date_from_iso
from src.utils.storage import JsonStorage


def test_app_renders_and_opens_day_dialog_without_exception(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"

    app = AppTest.from_file(str(app_file), default_timeout=30).run()

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "📅 排班日历",
        "⚖️ 方案对比与打分",
        "📊 数据统计",
        "⚙️ 基础配置",
    ]
    assert "生成预览" in [button.label for button in app.button]
    assert (tmp_path / "settings.json").exists()

    app.session_state["calendar.selected_date"] = date(2026, 11, 2)
    app.run()

    assert not app.exception
    assert "保存排班" in [button.label for button in app.button]
    assert "取消并关闭" in [button.label for button in app.button]
    assert "交接备注（可选）" in [area.label for area in app.text_area]


def test_config_dialog_and_plan_actions_through_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_file), default_timeout=30).run()

    next(button for button in app.button if button.label == "新增人员").click().run()
    app.text_input[0].input("测试新人")
    next(button for button in app.button if button.label == "保存").click().run()
    assert not app.exception
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert any(person["name"] == "测试新人" for person in settings["people"])

    original_plan_id = settings["active_plan_id"]
    next(button for button in app.button if button.label == "新建方案").click().run()
    app.text_input[0].input("自动化新方案")
    next(button for button in app.button if button.label == "确认").click().run()
    assert not app.exception

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["active_plan_id"] != original_plan_id
    assert len(list((tmp_path / "plans").glob("*.json"))) == 2

    old_index = next(
        index
        for index, label in enumerate(app.selectbox[0].options)
        if "示例排班" in label
    )
    app.selectbox[0].select_index(old_index).run()
    assert not app.exception
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["active_plan_id"] == original_plan_id


def test_leave_conflict_is_reported_and_cleanable_through_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    AppTest.from_file(str(app_file), default_timeout=30).run()

    storage = JsonStorage(tmp_path)
    settings = storage.load_settings()
    assert settings is not None and settings.active_plan_id is not None
    plan = storage.load_plan(settings.active_plan_id)
    assert plan is not None

    person = next(person for person in settings.people if person.is_active)
    shift = next(shift for shift in settings.shifts if shift.is_active)
    leave_date = date(2026, 11, 12)
    settings_with_leave = ConfigService.add_leave(
        settings,
        person_id=person.id,
        start_date=leave_date,
        end_date=leave_date,
        reason="自动化测试",
    )
    selected = {item.id: None for item in settings_with_leave.people}
    selected[person.id] = shift.id
    conflicting_day = ScheduleService.build_day(
        settings_with_leave,
        leave_date,
        selected,
        "",
    )
    updated_plan = ScheduleService.upsert_day(plan, conflicting_day)
    storage.save_settings(settings_with_leave)
    storage.save_plan(updated_plan)

    app = AppTest.from_file(str(app_file), default_timeout=30).run()
    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert str(metrics["健康分"]) == "0"
    cleanup = next(
        button
        for button in app.button
        if "预览并清理请假冲突" in button.label
    )
    cleanup.click().run()
    assert not app.exception
    confirm = next(button for button in app.button if button.label == "确认移除")
    confirm.click().run()
    assert not app.exception

    cleaned_settings = storage.load_settings()
    cleaned_plan = storage.load_plan(cleaned_settings.active_plan_id)
    assert cleaned_plan is not None
    assert all(
        not any(
            assignment.person_id == person.id
            for assignment in day.assignments
        )
        for day in cleaned_plan.days
    )


def test_comparison_section_renders_with_reference_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    AppTest.from_file(str(app_file), default_timeout=30).run()

    storage = JsonStorage(tmp_path)
    settings = storage.load_settings()
    assert settings is not None and settings.active_plan_id is not None
    active_plan = storage.load_plan(settings.active_plan_id)
    assert active_plan is not None
    reference = PlanService.duplicate_plan(active_plan, "参考方案")
    storage.save_plan(reference)

    app = AppTest.from_file(str(app_file), default_timeout=30).run()

    assert not app.exception
    assert "参考方案" in [select.label for select in app.selectbox]
    body_text = "\n".join(
        element.value
        for element in app.markdown
        if isinstance(element.value, str)
    )
    assert "方案差异对比" in body_text or any(
        tab.label == "⚖️ 方案对比与打分" for tab in app.tabs
    )


def test_statistics_chart_renders_with_schedule_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    AppTest.from_file(str(app_file), default_timeout=30).run()

    storage = JsonStorage(tmp_path)
    settings = storage.load_settings()
    assert settings is not None and settings.active_plan_id is not None
    plan = storage.load_plan(settings.active_plan_id)
    assert plan is not None
    person = next(person for person in settings.people if person.is_active)
    shift = next(shift for shift in settings.shifts if shift.is_active)
    selected = {item.id: None for item in settings.people}
    selected[person.id] = shift.id
    day = ScheduleService.build_day(
        settings,
        date(2026, 11, 4),
        selected,
        "",
    )
    storage.save_plan(ScheduleService.upsert_day(plan, day))

    app = AppTest.from_file(str(app_file), default_timeout=30).run()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert float(metrics["总加权负荷"]) > 0
    assert "加权负荷分布" in "\n".join(
        element.value for element in app.markdown if isinstance(element.value, str)
    )


def test_csv_import_creates_and_activates_a_new_plan_through_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    AppTest.from_file(str(app_file), default_timeout=30).run()

    storage = JsonStorage(tmp_path)
    settings = storage.load_settings()
    assert settings is not None and settings.active_plan_id is not None
    original_plan = storage.load_plan(settings.active_plan_id)
    assert original_plan is not None

    people = [person for person in settings.people if person.is_active]
    shifts = [shift for shift in settings.shifts if shift.is_active]
    assert len(people) >= 2 and shifts
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["日期", *(f"人员:{person.name}" for person in people), "交接备注"]
    )
    writer.writerow(
        [
            "2026-12-01",
            shifts[0].name,
            *(("",) * (len(people) - 1)),
            "CSV 导入备注",
        ]
    )
    writer.writerow(
        [
            "2026-12-02",
            *(["", shifts[0].name] + [""] * max(0, len(people) - 2)),
            "",
        ]
    )
    raw = buffer.getvalue().encode("utf-8-sig")

    app = AppTest.from_file(str(app_file), default_timeout=30).run()
    uploader = next(
        item
        for item in app.file_uploader
        if item.label == "上传 CSV（最大 2 MB）"
    )
    uploader.upload("导入测试.csv", raw, "text/csv").run()
    assert not app.exception
    assert any("校验通过" in item.value for item in app.success)

    name_input = next(
        item for item in app.text_input if item.key == "csv.plan_name"
    )
    name_input.input("CSV 自动化方案").run()
    assert not app.exception
    import_button = next(
        button for button in app.button if button.label == "导入为新方案"
    )
    import_button.click().run()
    assert not app.exception

    updated_settings = storage.load_settings()
    assert updated_settings is not None
    plans = storage.list_plans().plans
    imported = next(plan for plan in plans if plan.name == "CSV 自动化方案")
    assert len(plans) == 2
    assert updated_settings.active_plan_id == imported.id
    assert app.session_state["plan.selector"] == imported.id
    assert len(imported.days) == 2
    assert imported.days[0].note == "CSV 导入备注"
    assert imported.days[0].assignments[0].person_id == people[0].id

    kept_original = storage.load_plan(original_plan.id)
    assert kept_original is not None
    assert kept_original.days == original_plan.days


def test_coverage_group_dialog_renders_through_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_file), default_timeout=30).run()

    next(
        button
        for button in app.button
        if button.label == "新增覆盖组"
    ).click().run()
    assert not app.exception
    assert "覆盖组名称" in [item.label for item in app.text_input]
    assert "组内班次" in [item.label for item in app.multiselect]
    assert "组最低人数" in [item.label for item in app.number_input]


def test_sidebar_plan_switcher_changes_active_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    AppTest.from_file(str(app_file), default_timeout=30).run()

    storage = JsonStorage(tmp_path)
    settings = storage.load_settings()
    assert settings is not None
    alternate = PlanService.create_plan("备用方案")
    storage.save_plan(alternate)

    app = AppTest.from_file(str(app_file), default_timeout=30).run()
    next(button for button in app.button if button.label == "切换方案").click().run()
    assert not app.exception

    selector = next(item for item in app.selectbox if item.label == "目标方案")
    target_index = next(
        index
        for index, label in enumerate(selector.options)
        if "备用方案" in label
    )
    selector.select_index(target_index).run()
    switch_button = next(
        button for button in app.button if button.label == "切换到此方案"
    )
    switch_button.click().run()
    assert not app.exception

    updated = storage.load_settings()
    assert updated is not None
    assert updated.active_plan_id == alternate.id


def test_csv_import_validation_error_does_not_create_a_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SCHEDULE_DATA_DIR", str(tmp_path))
    app_file = Path(__file__).resolve().parents[1] / "app.py"
    app = AppTest.from_file(str(app_file), default_timeout=30).run()
    uploader = next(
        item
        for item in app.file_uploader
        if item.label == "上传 CSV（最大 2 MB）"
    )
    uploader.upload(
        "bad.csv",
        "日期,人员:不存在\n2026-11-02,早班\n".encode("utf-8"),
        "text/csv",
    ).run()

    assert not app.exception
    assert any("校验失败" in item.value for item in app.error)
    assert "导入为新方案" not in [button.label for button in app.button]
    assert len(list((tmp_path / "plans").glob("*.json"))) == 1


def test_local_date_from_iso_accepts_date_only() -> None:
    assert local_date_from_iso("2026-11-02") == date(2026, 11, 2)
