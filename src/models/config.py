"""人员、班次、请假与全局配置模型。"""

from __future__ import annotations

from datetime import date

from pydantic import Field, field_validator, model_validator

from src.models.common import StrictModel, new_id

_HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


class Person(StrictModel):
    """参与排班的人员。

    is_active=False 为软归档：不再参与新排班和模板，但历史方案中的引用保留，
    可随时恢复，不执行物理删除。
    """

    id: str = Field(default_factory=lambda: new_id("person"))
    name: str = Field(min_length=1, max_length=30)
    preferred_shift_ids: list[str] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("人员姓名不能为空")
        return value

    @field_validator("preferred_shift_ids")
    @classmethod
    def _deduplicate_preferences(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class Shift(StrictModel):
    """自定义班次。

    is_early / is_night 是硬约束的显式标记，避免依赖班次名称中的关键词。
    一个班次不能同时被标记为早班和夜班。is_active=False 仅影响新排班，
    不会改写历史方案。
    """

    id: str = Field(default_factory=lambda: new_id("shift"))
    name: str = Field(min_length=1, max_length=30)
    color: str = Field(default="#2563EB", pattern=_HEX_COLOR)
    load_weight: float = Field(default=1.0, gt=0, le=10)
    min_staff: int = Field(default=1, ge=0, le=99)
    is_early: bool = False
    is_night: bool = False
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("班次名称不能为空")
        return value

    @field_validator("color")
    @classmethod
    def _normalize_color(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _validate_flags(self) -> "Shift":
        if self.is_early and self.is_night:
            raise ValueError(f"班次“{self.name}”不能同时标记为早班和夜班")
        return self


class LeaveRecord(StrictModel):
    """按自然日记录的请假区间，起止日期均包含在请假范围内。"""

    id: str = Field(default_factory=lambda: new_id("leave"))
    person_id: str
    start_date: date
    end_date: date
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _validate_range(self) -> "LeaveRecord":
        if self.end_date < self.start_date:
            raise ValueError("请假结束日期不能早于开始日期")
        return self


class AppSettings(StrictModel):
    """跨方案共享的配置。修改人员或班次会影响所有方案的展示。"""

    schema_version: int = Field(default=1, ge=1)
    people: list[Person] = Field(default_factory=list)
    shifts: list[Shift] = Field(default_factory=list)
    leaves: list[LeaveRecord] = Field(default_factory=list)
    active_plan_id: str | None = None

    @model_validator(mode="after")
    def _validate_configuration(self) -> "AppSettings":
        person_ids = {person.id for person in self.people}
        shift_ids = {shift.id for shift in self.shifts}

        self._require_unique("人员 ID", [person.id for person in self.people])
        self._require_unique("人员姓名", [person.name.casefold() for person in self.people])
        self._require_unique("班次 ID", [shift.id for shift in self.shifts])
        self._require_unique("班次名称", [shift.name.casefold() for shift in self.shifts])
        self._require_unique("请假记录 ID", [leave.id for leave in self.leaves])

        for person in self.people:
            missing = set(person.preferred_shift_ids) - shift_ids
            if missing:
                raise ValueError(
                    f"人员“{person.name}”引用了不存在的偏好班次: {sorted(missing)}"
                )

        for leave in self.leaves:
            if leave.person_id not in person_ids:
                raise ValueError(
                    f"请假记录 {leave.id} 引用了不存在的人员: {leave.person_id}"
                )

        if self.active_plan_id is not None and not self.active_plan_id.strip():
            raise ValueError("active_plan_id 不能是空字符串")
        return self

    @staticmethod
    def _require_unique(label: str, values: list[str]) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        if duplicates:
            raise ValueError(f"{label}重复: {sorted(duplicates)}")
