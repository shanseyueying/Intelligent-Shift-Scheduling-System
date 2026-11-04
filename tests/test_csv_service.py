"""CSV 宽表导入导出的格式、校验和边界测试。"""

from __future__ import annotations

from datetime import date

import pytest

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    LeaveRecord,
    Person,
    SchedulePlan,
)
from src.services.constraint_service import ConstraintService
from src.services.csv_service import (
    CsvExportError,
    CsvImportError,
    CsvService,
)
from src.services.plan_service import PlanService
from tests.helpers import TEST_TIME


def _raw(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def test_export_roundtrip_preserves_assignments_and_note(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    data, filename = CsvService.export(
        base_settings,
        base_plan,
        today=date(2026, 11, 2),
    )

    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert text.splitlines()[0] == "日期,人员:张三,人员:李四,交接备注"
    assert filename == "排班_基础方案_20261102.csv"

    preview = CsvService.parse(
        base_settings,
        data,
        source_name=filename,
        now=TEST_TIME,
    )

    assert preview.day_count == 1
    assert preview.assignment_count == 2
    assert preview.plan.days[0].date == date(2026, 11, 2)
    assert preview.plan.days[0].note == "夜班交接设备钥匙"
    assert {
        (item.person_id, item.shift_id)
        for item in preview.plan.days[0].assignments
    } == {
        ("person_a", "shift_early"),
        ("person_b", "shift_night"),
    }
    assert preview.hard_violations == ()
    assert preview.warnings == ()
    assert preview.plan.history_audit[-1].action == "csv_imported"
    assert preview.plan.history_audit[-1].detail["source_name"] == filename


def test_import_accepts_gb18030_and_plain_person_headers(
    base_settings: AppSettings,
) -> None:
    raw = _raw(
        "日期,张三,李四,交接备注\n"
        "2026-11-03,休息,夜班,夜间注意消防通道\n"
        "2026-11-04,,早班,\n",
        encoding="gb18030",
    )

    preview = CsvService.parse(
        base_settings,
        raw,
        source_name="历史排班.csv",
    )

    assert preview.day_count == 2
    assert preview.assignment_count == 2
    first, second = preview.plan.days
    assert list(first.assignments) == [
        Assignment(person_id="person_b", shift_id="shift_night")
    ]
    assert first.note == "夜间注意消防通道"
    assert list(second.assignments) == [
        Assignment(person_id="person_b", shift_id="shift_early")
    ]


def test_export_quotes_note_with_comma_and_newline(
    base_settings: AppSettings,
) -> None:
    plan = PlanService.create_plan("特殊备注", now=TEST_TIME)
    day = DaySchedule(
        date=date(2026, 11, 5),
        assignments=[
            Assignment(person_id="person_a", shift_id="shift_early"),
        ],
        note="设备,钥匙\n已交接",
    )
    plan = PlanService.with_days(plan, [day], now=TEST_TIME)

    data, _ = CsvService.export(base_settings, plan)
    preview = CsvService.parse(
        base_settings,
        data,
        source_name="quoted.csv",
    )

    assert preview.plan.days[0].note == "设备,钥匙\n已交接"
    assert preview.plan.days[0].assignments == day.assignments


def test_import_warns_for_inactive_people_and_shifts(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    payload = base_settings.model_dump()
    payload["people"][1]["is_active"] = False
    payload["shifts"][2]["is_active"] = False
    settings = AppSettings.model_validate(payload)

    data, _ = CsvService.export(settings, base_plan)
    preview = CsvService.parse(
        settings,
        data,
        source_name="inactive.csv",
    )

    assert preview.inactive_people == ("李四",)
    assert preview.inactive_shifts == ("夜班",)
    assert any("已停用人员" in warning for warning in preview.warnings)
    assert any("已停用班次" in warning for warning in preview.warnings)
    assert preview.assignment_count == 2


def test_import_reports_hard_constraint_as_warning(
    base_settings: AppSettings,
) -> None:
    raw = _raw(
        "日期,人员:张三\n"
        "2026-11-02,夜班\n"
        "2026-11-03,早班\n"
    )

    preview = CsvService.parse(
        base_settings,
        raw,
        source_name="violation.csv",
    )

    assert len(preview.hard_violations) == 1
    assert preview.hard_violations[0].code == ConstraintService.NIGHT_THEN_EARLY
    assert any("硬约束违规" in warning for warning in preview.warnings)


def test_import_reports_leave_conflict_as_warning(
    base_settings: AppSettings,
) -> None:
    payload = base_settings.model_dump()
    payload["leaves"] = [
        LeaveRecord(
            person_id="person_a",
            start_date=date(2026, 11, 2),
            end_date=date(2026, 11, 2),
        ).model_dump()
    ]
    settings = AppSettings.model_validate(payload)
    raw = _raw("日期,人员:张三\n2026-11-02,早班\n")

    preview = CsvService.parse(
        settings,
        raw,
        source_name="leave.csv",
    )

    assert len(preview.hard_violations) == 1
    assert preview.hard_violations[0].code == ConstraintService.LEAVE_CONFLICT
    assert any("硬约束违规" in warning for warning in preview.warnings)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "为空"),
        (b"Date,person\n2026-11-02,early\n", "缺少必需列"),
        (
            _raw("日期,日期,人员:张三\n2026-11-02,2026-11-02,早班\n"),
            "重复列",
        ),
        (
            _raw("日期,人员:王五\n2026-11-02,早班\n"),
            "不在基础配置中",
        ),
        (
            _raw("日期,人员:张三,张三\n2026-11-02,早班,早班\n"),
            "出现了多个列",
        ),
        (
            _raw("日期,人员:张三\n2026-11-02,早班\n2026-11-02,中班\n"),
            "日期重复",
        ),
        (
            _raw("日期,人员:张三\n2026/11/02,早班\n"),
            "不是 YYYY-MM-DD",
        ),
        (
            _raw("日期,人员:张三\n2026-13-01,早班\n"),
            "日期“2026-13-01”无效",
        ),
        (
            _raw("日期,人员:张三\n2026-11-02,晚班\n"),
            "不存在",
        ),
        (
            _raw("日期,人员:张三\n2026-11-02,早班,多出来的一列\n"),
            "列数为 3",
        ),
        (
            _raw("日期,,人员:张三\n2026-11-02,,早班\n"),
            "表头为空",
        ),
    ],
)
def test_import_validation_errors(
    base_settings: AppSettings,
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(CsvImportError, match=message):
        CsvService.parse(base_settings, raw, source_name="bad.csv")


def test_import_rejects_too_many_columns_and_long_note(
    base_settings: AppSettings,
) -> None:
    with pytest.raises(CsvImportError, match="列数为 3"):
        CsvService.parse(
            base_settings,
            _raw("日期,人员:张三\n2026-11-02,早班,多余\n"),
            source_name="bad.csv",
        )

    with pytest.raises(CsvImportError, match="超过 1000 字"):
        CsvService.parse(
            base_settings,
            _raw(
                "日期,人员:张三,交接备注\n"
                f"2026-11-02,早班,{'备' * 1001}\n"
            ),
            source_name="bad.csv",
        )


def test_import_rejects_no_usable_rows(base_settings: AppSettings) -> None:
    with pytest.raises(CsvImportError, match="没有可导入"):
        CsvService.parse(
            base_settings,
            _raw("日期,人员:张三\n"),
            source_name="header-only.csv",
        )

    with pytest.raises(CsvImportError, match="没有可导入"):
        CsvService.parse(
            base_settings,
            _raw("日期,人员:张三\n2026-11-02,休息\n2026-11-03,\n"),
            source_name="rest-only.csv",
        )


def test_import_rejects_oversized_file(base_settings: AppSettings) -> None:
    with pytest.raises(CsvImportError, match="2 MB"):
        CsvService.parse(
            base_settings,
            b"a" * (2 * 1024 * 1024 + 1),
            source_name="huge.csv",
        )


def test_import_requires_a_person_column(base_settings: AppSettings) -> None:
    with pytest.raises(CsvImportError, match="至少需要一个人员列"):
        CsvService.parse(
            base_settings,
            _raw("日期,交接备注\n2026-11-02,备注\n"),
            source_name="no-people.csv",
        )


def test_export_rejects_unknown_references(
    base_settings: AppSettings,
) -> None:
    unknown_person_plan = PlanService.create_plan("坏引用", now=TEST_TIME)
    unknown_person_day = DaySchedule(
        date=date(2026, 11, 2),
        assignments=[Assignment(person_id="person_missing", shift_id="shift_early")],
    )
    unknown_person_plan = PlanService.with_days(
        unknown_person_plan,
        [unknown_person_day],
        now=TEST_TIME,
    )
    with pytest.raises(CsvExportError, match="不存在的人员"):
        CsvService.export(base_settings, unknown_person_plan)

    unknown_shift_plan = PlanService.create_plan("坏班次", now=TEST_TIME)
    unknown_shift_day = DaySchedule(
        date=date(2026, 11, 2),
        assignments=[Assignment(person_id="person_a", shift_id="shift_missing")],
    )
    unknown_shift_plan = PlanService.with_days(
        unknown_shift_plan,
        [unknown_shift_day],
        now=TEST_TIME,
    )
    with pytest.raises(CsvExportError, match="不存在的班次"):
        CsvService.export(base_settings, unknown_shift_plan)


def test_export_includes_all_active_people_and_sanitizes_filename(
    base_settings: AppSettings,
    base_plan: SchedulePlan,
) -> None:
    payload = base_settings.model_dump()
    payload["people"].append(Person(id="person_c", name="王五").model_dump())
    with_extra_person = AppSettings.model_validate(payload)
    renamed = PlanService.rename_plan(
        base_plan,
        '方案/测试:*?"<>|',
        now=TEST_TIME,
    )

    data, filename = CsvService.export(
        with_extra_person,
        renamed,
        today=date(2026, 11, 2),
    )

    header = data.decode("utf-8-sig").splitlines()[0]
    assert "人员:王五" in header
    assert "/" not in filename
    assert ":" not in filename
    assert "?" not in filename
    assert filename.startswith("排班_方案_测试")


def test_digests_and_suggested_name() -> None:
    assert CsvService.file_digest(b"abc") == CsvService.file_digest(b"abc")
    assert CsvService.file_digest(b"abc") != CsvService.file_digest(b"abd")
    assert (
        CsvService.suggested_plan_name("C:/fakepath/十月排班.csv")
        == "十月排班"
    )
    assert CsvService.suggested_plan_name("") == "CSV 导入"
