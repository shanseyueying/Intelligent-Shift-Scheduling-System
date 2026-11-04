"""JSON 存储的往返、错误隔离、原子写备份与路径安全测试。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.models import AppSettings, SchedulePlan
from src.services.plan_service import PlanService
from src.utils.storage import JsonStorage, StorageError
from tests.helpers import TEST_TIME


def test_settings_roundtrip(tmp_path: Path, base_settings: AppSettings) -> None:
    storage = JsonStorage(tmp_path)
    assert storage.load_settings() is None

    storage.save_settings(base_settings)

    loaded = storage.load_settings()
    assert loaded == base_settings


def test_plan_roundtrip_preserves_chinese_and_note(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    storage.save_plan(base_plan)

    loaded = storage.load_plan(base_plan.id)
    assert loaded == base_plan
    payload = (tmp_path / "plans" / f"{base_plan.id}.json").read_text(encoding="utf-8")
    assert "夜班交接设备钥匙" in payload


def test_invalid_settings_json_raises_storage_error(tmp_path: Path) -> None:
    storage = JsonStorage(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    storage.settings_path.write_text('{"people": [}', encoding="utf-8")

    with pytest.raises(StorageError, match="不是合法 JSON"):
        storage.load_settings()


def test_invalid_plan_field_raises_storage_error(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    plan_path = tmp_path / "plans" / f"{base_plan.id}.json"
    plan_path.parent.mkdir(parents=True)
    payload = base_plan.model_dump(mode="json")
    payload["unexpected"] = True
    plan_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(StorageError, match="校验失败"):
        storage.load_plan(base_plan.id)


def test_second_save_backs_up_previous_version(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    storage.save_plan(base_plan)

    renamed = PlanService.rename_plan(
        base_plan,
        "改名后的方案",
        now=TEST_TIME + timedelta(minutes=1),
    )
    storage.save_plan(renamed)

    backups = sorted(storage.backups_dir.glob(f"{base_plan.id}__*.json"))
    assert len(backups) == 1
    backed_up = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backed_up["name"] == base_plan.name
    assert storage.load_plan(base_plan.id).name == "改名后的方案"


def test_backup_retention_caps_each_file(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    plan = base_plan
    for index in range(12):
        plan = PlanService.rename_plan(
            plan,
            f"版本 {index}",
            now=TEST_TIME + timedelta(minutes=index),
        )
        storage.save_plan(plan)

    backups = list(storage.backups_dir.glob(f"{base_plan.id}__*.json"))
    assert len(backups) == storage.MAX_BACKUPS_PER_FILE


def test_list_plans_isolates_corrupt_file(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    storage.save_plan(base_plan)
    corrupt_path = tmp_path / "plans" / "plan_corrupt.json"
    corrupt_path.write_text("{oops", encoding="utf-8")

    inventory = storage.list_plans()

    assert [plan.id for plan in inventory.plans] == [base_plan.id]
    assert len(inventory.errors) == 1
    assert "plan_corrupt.json" in inventory.errors[0]
    assert inventory.has_any_file is True


def test_unsafe_plan_id_is_rejected(
    tmp_path: Path,
    base_plan: SchedulePlan,
) -> None:
    storage = JsonStorage(tmp_path)
    with pytest.raises(StorageError, match="非法方案 ID"):
        storage.load_plan("../settings")

    unsafe = base_plan.model_copy(update={"id": "../escape"})
    with pytest.raises(StorageError, match="非法方案 ID"):
        storage.save_plan(unsafe)
    assert not (tmp_path / "escape.json").exists()


def test_empty_file_reports_interrupted_write(tmp_path: Path) -> None:
    storage = JsonStorage(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    storage.settings_path.write_text("", encoding="utf-8")

    with pytest.raises(StorageError, match="空文件"):
        storage.load_settings()
