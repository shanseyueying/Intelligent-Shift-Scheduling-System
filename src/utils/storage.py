"""本地 JSON 持久化：校验、原子写入与滚动备份。

设计取舍：
- 配置和每个方案分文件保存，缩小单次写入的故障影响面。
- 先写同目录临时文件并 fsync，再 os.replace，避免半截 JSON。
- 覆盖前复制旧版本到 backups/，每个目标最多保留 10 份。
- 读取失败统一抛出 StorageError，由 UI 转成可读提示，绝不让脏数据导致崩溃。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from src.models import AppSettings, SchedulePlan
from src.models.common import is_safe_id

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"


class StorageError(RuntimeError):
    """可展示给用户的存储异常；保留底层异常链便于调试。"""


@dataclass(frozen=True)
class PlanInventory:
    """方案扫描结果。损坏文件只影响自身，不会阻断可用方案的加载。"""

    plans: list[SchedulePlan] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_any_file(self) -> bool:
        return bool(self.plans or self.errors)


class JsonStorage:
    """配置文件 + data/plans/<plan_id>.json 的本地存储。"""

    MAX_BACKUPS_PER_FILE = 10

    def __init__(self, data_dir: str | Path | None = None) -> None:
        raw_dir = data_dir or os.environ.get("SCHEDULE_DATA_DIR") or _DEFAULT_DATA_DIR
        self.data_dir = Path(raw_dir).expanduser().resolve()
        self.plans_dir = self.data_dir / "plans"
        self.backups_dir = self.data_dir / "backups"
        self.settings_path = self.data_dir / "settings.json"

    # ---------- 公共读取接口 ----------

    def load_settings(self) -> AppSettings | None:
        """设置文件不存在时返回 None；存在但损坏时抛出 StorageError。"""
        payload = self._read_json(self.settings_path)
        if payload is None:
            return None
        try:
            return AppSettings.model_validate(payload)
        except ValidationError as exc:
            raise StorageError(
                f"设置文件校验失败：{self._format_validation_error(exc)}"
            ) from exc

    def load_plan(self, plan_id: str) -> SchedulePlan | None:
        path = self._plan_path(plan_id)
        payload = self._read_json(path)
        if payload is None:
            return None
        try:
            plan = SchedulePlan.model_validate(payload)
        except ValidationError as exc:
            raise StorageError(
                f"方案文件 {path.name} 校验失败：{self._format_validation_error(exc)}"
            ) from exc
        if plan.id != plan_id:
            raise StorageError(
                f"方案文件 {path.name} 内的 ID（{plan.id}）与文件名不一致"
            )
        return plan

    def list_plans(self) -> PlanInventory:
        """加载所有可读方案，同时收集其他方案的错误，按更新时间倒序。"""
        if not self.plans_dir.exists():
            return PlanInventory()

        plans: list[SchedulePlan] = []
        errors: list[str] = []
        for path in sorted(self.plans_dir.glob("*.json")):
            try:
                payload = self._read_json(path)
                if payload is None:
                    continue
                plan = SchedulePlan.model_validate(payload)
                if plan.id != path.stem:
                    raise StorageError(
                        f"方案文件 {path.name} 内的 ID（{plan.id}）与文件名不一致"
                    )
                plans.append(plan)
            except (StorageError, ValidationError) as exc:
                if isinstance(exc, ValidationError):
                    message = self._format_validation_error(exc)
                else:
                    message = str(exc)
                errors.append(f"{path.name}: {message}")

        plans.sort(key=lambda plan: plan.updated_at, reverse=True)
        return PlanInventory(plans=plans, errors=errors)

    # ---------- 公共写入接口 ----------

    def save_settings(self, settings: AppSettings) -> None:
        self._atomic_write(self.settings_path, settings.model_dump(mode="json"))

    def save_plan(self, plan: SchedulePlan) -> None:
        self._atomic_write(self._plan_path(plan.id), plan.model_dump(mode="json"))

    # ---------- 路径与备份 ----------

    def _plan_path(self, plan_id: str) -> Path:
        if not is_safe_id(plan_id):
            raise StorageError(f"非法方案 ID，拒绝访问文件: {plan_id!r}")
        return self.plans_dir / f"{plan_id}.json"

    def _read_json(self, path: Path) -> dict | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise StorageError(f"无法读取 {path.name}: {exc}") from exc

        if not raw.strip():
            raise StorageError(f"{path.name} 是空文件，可能是写入中断")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"{path.name} 不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）"
            ) from exc
        if not isinstance(payload, dict):
            raise StorageError(f"{path.name} 的顶层结构必须是 JSON 对象")
        return payload

    def _atomic_write(self, target: Path, payload: dict) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"无法创建数据目录 {target.parent}: {exc}") from exc

        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.stem}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            if target.exists():
                self._backup_existing(target)
            os.replace(temp_path, target)
            temp_path = None
        except StorageError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise StorageError(f"写入 {target.name} 失败: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _backup_existing(self, target: Path) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S_%f")
        backup_base = f"{target.stem}__{stamp}"
        backup_path = self.backups_dir / f"{backup_base}{target.suffix}"
        # Windows 时钟精度可能让连续备份得到同一时间戳；追加序号避免互相覆盖。
        suffix_index = 1
        while backup_path.exists():
            backup_path = self.backups_dir / (
                f"{backup_base}_{suffix_index}{target.suffix}"
            )
            suffix_index += 1
        try:
            shutil.copy2(target, backup_path)
        except OSError as exc:
            raise StorageError(f"备份 {target.name} 失败，已取消本次写入: {exc}") from exc
        self._prune_backups(target.stem)

    def _prune_backups(self, stem: str) -> None:
        backups = sorted(self.backups_dir.glob(f"{stem}__*.json"))
        for stale in backups[: -self.MAX_BACKUPS_PER_FILE]:
            try:
                stale.unlink()
            except OSError:
                continue

    @staticmethod
    def _format_validation_error(exc: ValidationError) -> str:
        messages: list[str] = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"{location or '<root>'}: {error['msg']}")
        suffix = "（仅显示前 5 条）" if len(exc.errors()) > 5 else ""
        return "; ".join(messages) + suffix
