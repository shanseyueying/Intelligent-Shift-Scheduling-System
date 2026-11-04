"""基础配置的纯业务操作。

所有方法都返回新的 AppSettings，不修改传入对象，也不直接访问磁盘。
停用人员/班次采用软归档，历史和方案中的 ID 引用保持有效。
"""

from __future__ import annotations

from datetime import date

from pydantic import ValidationError

from src.models import AppSettings, LeaveRecord, Person, Shift


class ConfigServiceError(ValueError):
    """可直接展示给用户的配置校验错误。"""


class ConfigService:
    # ---------- 人员 ----------

    @staticmethod
    def add_person(
        settings: AppSettings,
        *,
        name: str,
        preferred_shift_ids: list[str],
    ) -> AppSettings:
        try:
            person = Person(
                name=name,
                preferred_shift_ids=preferred_shift_ids,
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _rebuild(settings, people=[*settings.people, person])

    @staticmethod
    def update_person(
        settings: AppSettings,
        person_id: str,
        *,
        name: str,
        preferred_shift_ids: list[str],
    ) -> AppSettings:
        person = _find_person(settings, person_id)
        try:
            updated = Person.model_validate(
                {
                    **person.model_dump(),
                    "name": name,
                    "preferred_shift_ids": preferred_shift_ids,
                }
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _replace_person(settings, updated)

    @staticmethod
    def set_person_active(
        settings: AppSettings,
        person_id: str,
        is_active: bool,
    ) -> AppSettings:
        person = _find_person(settings, person_id)
        try:
            updated = Person.model_validate(
                {**person.model_dump(), "is_active": is_active}
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _replace_person(settings, updated)

    # ---------- 班次 ----------

    @staticmethod
    def add_shift(
        settings: AppSettings,
        *,
        name: str,
        color: str,
        load_weight: float,
        is_early: bool,
        is_night: bool,
        min_staff: int = 1,
    ) -> AppSettings:
        try:
            shift = Shift(
                name=name,
                color=color,
                load_weight=load_weight,
                min_staff=min_staff,
                is_early=is_early,
                is_night=is_night,
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _rebuild(settings, shifts=[*settings.shifts, shift])

    @staticmethod
    def update_shift(
        settings: AppSettings,
        shift_id: str,
        *,
        name: str,
        color: str,
        load_weight: float,
        is_early: bool,
        is_night: bool,
        min_staff: int = 1,
    ) -> AppSettings:
        shift = _find_shift(settings, shift_id)
        try:
            updated = Shift.model_validate(
                {
                    **shift.model_dump(),
                    "name": name,
                    "color": color,
                    "load_weight": load_weight,
                    "min_staff": min_staff,
                    "is_early": is_early,
                    "is_night": is_night,
                }
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _replace_shift(settings, updated)

    @staticmethod
    def set_shift_active(
        settings: AppSettings,
        shift_id: str,
        is_active: bool,
    ) -> AppSettings:
        shift = _find_shift(settings, shift_id)
        try:
            updated = Shift.model_validate(
                {**shift.model_dump(), "is_active": is_active}
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _replace_shift(settings, updated)

    # ---------- 请假 ----------

    @staticmethod
    def add_leave(
        settings: AppSettings,
        *,
        person_id: str,
        start_date: date,
        end_date: date,
        reason: str,
    ) -> AppSettings:
        try:
            leave = LeaveRecord(
                person_id=person_id,
                start_date=start_date,
                end_date=end_date,
                reason=reason,
            )
        except ValidationError as exc:
            raise ConfigServiceError(_format_validation_error(exc)) from exc
        return _rebuild(settings, leaves=[*settings.leaves, leave])

    @staticmethod
    def delete_leave(settings: AppSettings, leave_id: str) -> AppSettings:
        if not any(leave.id == leave_id for leave in settings.leaves):
            raise ConfigServiceError("要删除的请假记录不存在")
        remaining = [leave for leave in settings.leaves if leave.id != leave_id]
        return _rebuild(settings, leaves=remaining)


def _rebuild(settings: AppSettings, **changes: object) -> AppSettings:
    payload = settings.model_dump()
    payload.update(changes)
    try:
        return AppSettings.model_validate(payload)
    except ValidationError as exc:
        raise ConfigServiceError(_format_validation_error(exc)) from exc


def _find_person(settings: AppSettings, person_id: str) -> Person:
    person = next((item for item in settings.people if item.id == person_id), None)
    if person is None:
        raise ConfigServiceError("人员不存在，可能已被其他操作修改")
    return person


def _find_shift(settings: AppSettings, shift_id: str) -> Shift:
    shift = next((item for item in settings.shifts if item.id == shift_id), None)
    if shift is None:
        raise ConfigServiceError("班次不存在，可能已被其他操作修改")
    return shift


def _replace_person(settings: AppSettings, person: Person) -> AppSettings:
    people = [person if item.id == person.id else item for item in settings.people]
    return _rebuild(settings, people=people)


def _replace_shift(settings: AppSettings, shift: Shift) -> AppSettings:
    shifts = [shift if item.id == shift.id else item for item in settings.shifts]
    return _rebuild(settings, shifts=shifts)


def _format_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"])
        message = str(error["msg"]).removeprefix("Value error, ")
        messages.append(f"{location or '<root>'}: {message}")
    return "；".join(messages)
