"""基础配置服务的增改、软归档和请假操作。"""

from __future__ import annotations

from datetime import date

import pytest

from src.models import AppSettings
from src.services.config_service import ConfigService, ConfigServiceError


def test_add_and_update_person_returns_new_settings(
    base_settings: AppSettings,
) -> None:
    added = ConfigService.add_person(
        base_settings,
        name="王五",
        preferred_shift_ids=["shift_middle"],
    )
    assert len(added.people) == 3
    assert len(base_settings.people) == 2
    assert added.active_plan_id == base_settings.active_plan_id

    new_person = next(person for person in added.people if person.name == "王五")
    assert new_person.is_active is True

    updated = ConfigService.update_person(
        added,
        new_person.id,
        name="王五五",
        preferred_shift_ids=["shift_night"],
    )
    changed = next(person for person in updated.people if person.id == new_person.id)
    assert changed.name == "王五五"
    assert changed.preferred_shift_ids == ["shift_night"]
    assert changed.is_active is True
    assert next(person for person in added.people if person.id == new_person.id).name == "王五"


def test_duplicate_person_name_is_rejected(base_settings: AppSettings) -> None:
    with pytest.raises(ConfigServiceError, match="人员姓名重复"):
        ConfigService.add_person(
            base_settings,
            name="张三",
            preferred_shift_ids=[],
        )


def test_person_soft_archive_and_restore(base_settings: AppSettings) -> None:
    archived = ConfigService.set_person_active(base_settings, "person_a", False)
    person = next(item for item in archived.people if item.id == "person_a")

    assert person.is_active is False
    assert len(archived.people) == 2
    assert next(item for item in base_settings.people if item.id == "person_a").is_active

    restored = ConfigService.set_person_active(archived, "person_a", True)
    assert next(item for item in restored.people if item.id == "person_a").is_active


def test_shift_add_update_and_archive(base_settings: AppSettings) -> None:
    added = ConfigService.add_shift(
        base_settings,
        name="凌晨班",
        color="#112233",
        load_weight=1.8,
        min_staff=2,
        is_early=False,
        is_night=True,
    )
    shift = next(item for item in added.shifts if item.name == "凌晨班")
    assert shift.is_active is True
    assert shift.color == "#112233"
    assert shift.min_staff == 2

    updated = ConfigService.update_shift(
        added,
        shift.id,
        name="深夜班",
        color="#445566",
        load_weight=2.0,
        min_staff=3,
        is_early=False,
        is_night=True,
    )
    changed = next(item for item in updated.shifts if item.id == shift.id)
    assert changed.name == "深夜班"
    assert changed.min_staff == 3
    assert next(item for item in added.shifts if item.id == shift.id).name == "凌晨班"

    archived = ConfigService.set_shift_active(updated, shift.id, False)
    assert next(item for item in archived.shifts if item.id == shift.id).is_active is False


def test_shift_cannot_be_both_early_and_night(base_settings: AppSettings) -> None:
    with pytest.raises(ConfigServiceError, match="不能同时"):
        ConfigService.add_shift(
            base_settings,
            name="冲突班",
            color="#112233",
            load_weight=1.0,
            is_early=True,
            is_night=True,
        )


def test_coverage_group_add_update_and_archive(
    base_settings: AppSettings,
) -> None:
    added = ConfigService.add_coverage_group(
        base_settings,
        name="日班组",
        min_staff=2,
        shift_ids=["shift_early", "shift_middle"],
    )
    group = next(
        item for item in added.coverage_groups if item.name == "日班组"
    )
    assert group.is_active is True
    assert group.min_staff == 2
    assert group.shift_ids == ["shift_early", "shift_middle"]

    updated = ConfigService.update_coverage_group(
        added,
        group.id,
        name="白班覆盖组",
        min_staff=1,
        shift_ids=["shift_early"],
    )
    changed = next(
        item for item in updated.coverage_groups if item.id == group.id
    )
    assert changed.name == "白班覆盖组"
    assert changed.shift_ids == ["shift_early"]
    assert next(
        item for item in added.coverage_groups if item.id == group.id
    ).name == "日班组"

    archived = ConfigService.set_coverage_group_active(updated, group.id, False)
    assert next(
        item for item in archived.coverage_groups if item.id == group.id
    ).is_active is False


def test_coverage_group_rejects_overlap_and_duplicate_name(
    base_settings: AppSettings,
) -> None:
    added = ConfigService.add_coverage_group(
        base_settings,
        name="日班组",
        min_staff=1,
        shift_ids=["shift_early", "shift_middle"],
    )
    with pytest.raises(ConfigServiceError, match="多个启用中的覆盖组"):
        ConfigService.add_coverage_group(
            added,
            name="重叠组",
            min_staff=1,
            shift_ids=["shift_middle", "shift_night"],
        )
    with pytest.raises(ConfigServiceError, match="覆盖组名称重复"):
        ConfigService.add_coverage_group(
            added,
            name="日班组",
            min_staff=1,
            shift_ids=["shift_night"],
        )
    with pytest.raises(ConfigServiceError, match="覆盖组不存在"):
        ConfigService.set_coverage_group_active(added, "coverage_missing", False)


def test_leave_add_and_delete(base_settings: AppSettings) -> None:
    added = ConfigService.add_leave(
        base_settings,
        person_id="person_a",
        start_date=date(2026, 11, 5),
        end_date=date(2026, 11, 7),
        reason="家中有事",
    )
    assert len(added.leaves) == 1
    assert len(base_settings.leaves) == 0

    leave = added.leaves[0]
    deleted = ConfigService.delete_leave(added, leave.id)
    assert deleted.leaves == []


def test_leave_for_unknown_person_is_rejected(base_settings: AppSettings) -> None:
    with pytest.raises(ConfigServiceError, match="不存在的人员"):
        ConfigService.add_leave(
            base_settings,
            person_id="person_missing",
            start_date=date(2026, 11, 5),
            end_date=date(2026, 11, 7),
            reason="",
        )


def test_leave_reversed_dates_are_rejected(base_settings: AppSettings) -> None:
    with pytest.raises(ConfigServiceError, match="结束日期不能早于"):
        ConfigService.add_leave(
            base_settings,
            person_id="person_a",
            start_date=date(2026, 11, 7),
            end_date=date(2026, 11, 5),
            reason="",
        )


def test_missing_entities_raise_clear_errors(base_settings: AppSettings) -> None:
    with pytest.raises(ConfigServiceError, match="人员不存在"):
        ConfigService.update_person(
            base_settings,
            "person_missing",
            name="新名字",
            preferred_shift_ids=[],
        )
    with pytest.raises(ConfigServiceError, match="班次不存在"):
        ConfigService.set_shift_active(base_settings, "shift_missing", False)
    with pytest.raises(ConfigServiceError, match="不存在"):
        ConfigService.delete_leave(base_settings, "leave_missing")
