"""排班 CSV 导入导出。

格式为宽表：日期 + 人员列 + 交接备注。人员列导出使用“人员:姓名”前缀，
避免与固定列重名；导入同时接受没有前缀的已知人员姓名。
空白格表示休息；导入允许已知的停用人员和班次，但会给出警告。
CSV 校验只负责结构、引用和日期，硬约束违规作为导入预览警告，不阻止创建新方案。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from src.models import (
    AppSettings,
    Assignment,
    DaySchedule,
    HistoryAuditEntry,
    Person,
    SchedulePlan,
)
from src.models.common import utc_now
from src.services.constraint_service import ConstraintService, HardConstraintViolation
from src.services.plan_service import PlanService

_DATE_COLUMN = "日期"
_NOTE_COLUMN = "交接备注"
_PERSON_PREFIX = "人员:"
_REST_TOKENS = {"休息", "休"}
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 5000
_MAX_ERRORS = 30


class CsvImportError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors[:_MAX_ERRORS])
        if len(errors) > _MAX_ERRORS:
            self.errors += (f"还有 {len(errors) - _MAX_ERRORS} 个错误未显示",)
        super().__init__("；".join(self.errors))


class CsvExportError(ValueError):
    """当前方案包含无法安全导出的脏引用。"""


@dataclass(frozen=True)
class CsvImportPreview:
    plan: SchedulePlan
    source_name: str
    day_count: int
    assignment_count: int
    inactive_people: tuple[str, ...]
    inactive_shifts: tuple[str, ...]
    hard_violations: tuple[HardConstraintViolation, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        messages: list[str] = []
        if self.inactive_people:
            messages.append(
                "CSV 包含已停用人员：" + "、".join(self.inactive_people)
            )
        if self.inactive_shifts:
            messages.append(
                "CSV 使用了已停用班次：" + "、".join(self.inactive_shifts)
            )
        if self.hard_violations:
            messages.append(
                f"导入后将产生 {len(self.hard_violations)} 个硬约束违规，"
                "健康分将显示为“不合格”。"
            )
        return tuple(messages)


class CsvService:
    @classmethod
    def parse(
        cls,
        settings: AppSettings,
        raw: bytes,
        *,
        source_name: str,
        now: datetime | None = None,
    ) -> CsvImportPreview:
        if not raw:
            raise CsvImportError(["CSV 文件为空"])
        if len(raw) > _MAX_FILE_BYTES:
            raise CsvImportError(
                [f"CSV 文件超过 {_MAX_FILE_BYTES // (1024 * 1024)} MB 限制"]
            )

        text = cls._decode(raw)
        try:
            reader = csv.reader(io.StringIO(text))
            raw_rows = [row for row in reader]
        except csv.Error as exc:
            raise CsvImportError([f"CSV 解析失败：{exc}"]) from exc

        rows = [row for row in raw_rows if any(cell.strip() for cell in row)]
        if not rows:
            raise CsvImportError(["CSV 没有表头或数据"])
        if len(rows) - 1 > _MAX_ROWS:
            raise CsvImportError(
                [f"CSV 数据行超过 {_MAX_ROWS} 行限制"]
            )

        headers = [cell.strip() for cell in rows[0]]
        if len(set(headers)) != len(headers):
            raise CsvImportError(["CSV 表头存在重复列"])
        if _DATE_COLUMN not in headers:
            raise CsvImportError([f"缺少必需列“{_DATE_COLUMN}”"])

        date_index = headers.index(_DATE_COLUMN)
        note_index = (
            headers.index(_NOTE_COLUMN) if _NOTE_COLUMN in headers else None
        )
        people_by_name = {person.name: person for person in settings.people}
        shift_by_name = {shift.name: shift for shift in settings.shifts}
        person_columns: dict[int, Person] = {}
        inactive_people: list[str] = []

        for index, header in enumerate(headers):
            if index in {date_index, note_index}:
                continue
            if not header:
                raise CsvImportError(
                    [f"第 {index + 1} 列的表头为空"]
                )
            if header.startswith(_PERSON_PREFIX):
                person_name = header[len(_PERSON_PREFIX) :].strip()
            else:
                person_name = header
            person = people_by_name.get(person_name)
            if person is None:
                raise CsvImportError(
                    [f"人员列“{person_name}”不在基础配置中"]
                )
            if any(item.id == person.id for item in person_columns.values()):
                raise CsvImportError(
                    [f"人员“{person.name}”出现了多个列"]
                )
            person_columns[index] = person
            if not person.is_active:
                inactive_people.append(person.name)

        if not person_columns:
            raise CsvImportError(["CSV 至少需要一个人员列"])

        days: list[DaySchedule] = []
        errors: list[str] = []
        seen_dates: set[date] = set()
        inactive_shifts: list[str] = []

        for row_number, row in enumerate(rows[1:], start=2):
            if len(row) != len(headers):
                errors.append(
                    f"第 {row_number} 行列数为 {len(row)}，应为 {len(headers)}"
                )
                continue

            date_text = row[date_index].strip()
            if not _ISO_DATE_PATTERN.fullmatch(date_text):
                errors.append(
                    f"第 {row_number} 行日期“{date_text}”不是 YYYY-MM-DD"
                )
                continue
            try:
                on_date = date.fromisoformat(date_text)
            except ValueError:
                errors.append(
                    f"第 {row_number} 行日期“{date_text}”无效"
                )
                continue
            if on_date in seen_dates:
                errors.append(f"第 {row_number} 行日期重复：{date_text}")
                continue
            seen_dates.add(on_date)

            note = (
                row[note_index].strip() if note_index is not None else ""
            )
            if len(note) > 1000:
                errors.append(
                    f"第 {row_number} 行交接备注超过 1000 字"
                )
                continue

            assignments: list[Assignment] = []
            row_has_error = False
            for index, person in person_columns.items():
                cell = row[index].strip()
                if not cell:
                    continue
                shift = shift_by_name.get(cell)
                if shift is None:
                    if cell in _REST_TOKENS:
                        continue
                    errors.append(
                        f"第 {row_number} 行人员“{person.name}”的班次"
                        f"“{cell}”不存在；留空表示休息"
                    )
                    row_has_error = True
                    continue
                if not shift.is_active and shift.name not in inactive_shifts:
                    inactive_shifts.append(shift.name)
                assignments.append(
                    Assignment(
                        person_id=person.id,
                        shift_id=shift.id,
                    )
                )

            if row_has_error:
                continue
            if not assignments and not note:
                continue
            try:
                days.append(
                    DaySchedule(
                        date=on_date,
                        assignments=assignments,
                        note=note,
                    )
                )
            except ValidationError as exc:
                errors.append(
                    f"第 {row_number} 行数据校验失败：{cls._validation_text(exc)}"
                )

        if errors:
            raise CsvImportError(errors)
        if not days:
            raise CsvImportError(["CSV 没有可导入的排班数据"])

        days.sort(key=lambda item: item.date)
        timestamp = now or utc_now()
        plan = PlanService.create_plan("CSV 导入预览", now=timestamp)
        plan = PlanService.with_days(plan, days, now=timestamp)
        assignment_count = sum(len(day.assignments) for day in plan.days)

        payload = plan.model_dump()
        audit = [entry.model_dump() for entry in plan.history_audit]
        audit.append(
            HistoryAuditEntry(
                timestamp=timestamp,
                action="csv_imported",
                detail={
                    "source_name": source_name,
                    "day_count": len(plan.days),
                    "assignment_count": assignment_count,
                },
            ).model_dump()
        )
        payload["history_audit"] = audit
        plan = SchedulePlan.model_validate(payload)

        hard_violations = ConstraintService.find_hard_violations(settings, plan)
        inactive_people_sorted = [
            person.name
            for person in settings.people
            if person.name in inactive_people
        ]
        inactive_shifts_sorted = [
            shift.name
            for shift in settings.shifts
            if shift.name in inactive_shifts
        ]

        return CsvImportPreview(
            plan=plan,
            source_name=source_name,
            day_count=len(plan.days),
            assignment_count=assignment_count,
            inactive_people=tuple(inactive_people_sorted),
            inactive_shifts=tuple(inactive_shifts_sorted),
            hard_violations=hard_violations,
        )

    @classmethod
    def export(
        cls,
        settings: AppSettings,
        plan: SchedulePlan,
        *,
        today: date | None = None,
    ) -> tuple[bytes, str]:
        person_by_id = {person.id: person for person in settings.people}
        shift_by_id = {shift.id: shift for shift in settings.shifts}
        referenced_people = {
            assignment.person_id
            for day in plan.days
            for assignment in day.assignments
        }
        missing_people = referenced_people - set(person_by_id)
        if missing_people:
            raise CsvExportError(
                f"方案引用了不存在的人员：{sorted(missing_people)}"
            )

        people = [
            person
            for person in settings.people
            if person.is_active or person.id in referenced_people
        ]
        headers = [_DATE_COLUMN]
        headers.extend(f"{_PERSON_PREFIX}{person.name}" for person in people)
        headers.append(_NOTE_COLUMN)

        rows: list[list[str]] = []
        for day in sorted(plan.days, key=lambda item: item.date):
            shift_by_person = {
                assignment.person_id: assignment.shift_id
                for assignment in day.assignments
            }
            unknown_shifts = set(shift_by_person.values()) - set(shift_by_id)
            if unknown_shifts:
                raise CsvExportError(
                    f"{day.date.isoformat()} 引用了不存在的班次："
                    f"{sorted(unknown_shifts)}"
                )
            row = [day.date.isoformat()]
            row.extend(
                shift_by_id[shift_by_person[person.id]].name
                if person.id in shift_by_person
                else ""
                for person in people
            )
            row.append(day.note)
            rows.append(row)

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)
        data = buffer.getvalue().encode("utf-8-sig")

        safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", plan.name)
        safe_name = safe_name.strip(" ._")[:60] or "方案"
        export_date = today or date.today()
        filename = f"排班_{safe_name}_{export_date:%Y%m%d}.csv"
        return data, filename

    @staticmethod
    def file_digest(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def settings_digest(settings: AppSettings) -> str:
        return hashlib.sha256(
            settings.model_dump_json().encode("utf-8")
        ).hexdigest()

    @staticmethod
    def suggested_plan_name(source_name: str) -> str:
        stem = Path(source_name).stem.strip() or "CSV 导入"
        stem = stem[:40]
        return stem

    @staticmethod
    def _decode(raw: bytes) -> str:
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise CsvImportError(
            ["无法解码 CSV 文件，请使用 UTF-8（推荐）或 GB18030 编码"]
        )

    @staticmethod
    def _validation_text(exc: ValidationError) -> str:
        messages = []
        for error in exc.errors()[:3]:
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"{location or '<root>'}: {error['msg']}")
        return "；".join(messages)
