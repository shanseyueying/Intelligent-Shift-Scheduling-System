"""排班方案、每日安排与审计预留字段。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from src.models.common import StrictModel, new_id, utc_now


class Assignment(StrictModel):
    """某人在某天执行的一个班次；不含日期，由所属 DaySchedule 提供。"""

    person_id: str
    shift_id: str


class DaySchedule(StrictModel):
    """一天内的所有安排和一条交接备注。

    “单人单日一个班次”在模型层直接成立：assignments 中不允许重复 person_id。
    """

    date: date
    assignments: list[Assignment] = Field(default_factory=list)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _validate_one_shift_per_person(self) -> "DaySchedule":
        person_ids = [assignment.person_id for assignment in self.assignments]
        duplicates = {
            person_id
            for person_id in person_ids
            if person_ids.count(person_id) > 1
        }
        if duplicates:
            raise ValueError(
                f"{self.date.isoformat()} 同一天存在重复排班人员: {sorted(duplicates)}"
            )
        return self


class HistoryAuditEntry(StrictModel):
    """为将来“跨月历史公平性复盘”预留的审计事件。"""

    timestamp: datetime = Field(default_factory=utc_now)
    action: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("审计时间必须包含时区")
        return value


class SchedulePlan(StrictModel):
    """一个完整排班版本。方案复制时对 days 做深拷贝，编辑互不影响。"""

    schema_version: int = Field(default=1, ge=1)
    id: str = Field(default_factory=lambda: new_id("plan"))
    name: str = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    days: list[DaySchedule] = Field(default_factory=list)
    history_audit: list[HistoryAuditEntry] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("方案名称不能为空")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("方案时间必须包含时区")
        return value

    @model_validator(mode="after")
    def _validate_days(self) -> "SchedulePlan":
        dates = [day.date for day in self.days]
        duplicates = sorted(
            {day_date for day_date in dates if dates.count(day_date) > 1}
        )
        if duplicates:
            raise ValueError(f"方案中存在重复日期: {duplicates}")
        if self.updated_at < self.created_at:
            raise ValueError("方案更新时间不能早于创建时间")
        return self

    @property
    def day_map(self) -> dict[date, DaySchedule]:
        """UI 只读查询用；修改时请通过服务层返回新对象。"""
        return {day.date: day for day in self.days}
