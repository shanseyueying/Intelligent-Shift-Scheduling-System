"""模型层共享的 ID、时间与基础配置。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

_ID_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def new_id(prefix: str) -> str:
    """生成带业务前缀、可安全用于文件名的短 ID。"""
    if not _ID_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError(f"非法 ID 前缀: {prefix!r}")
    return f"{prefix}_{uuid4().hex[:12]}"


def is_safe_id(value: str) -> bool:
    """拒绝路径分隔符等不适合作为存储文件名的 ID。"""
    return bool(_SAFE_ID_PATTERN.fullmatch(value))


def utc_now() -> datetime:
    """统一使用带时区的 UTC 时间，JSON 中可无损往返。"""
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """所有领域模型共用的严格校验策略。

    - extra=forbid：脏 JSON 中未知字段会显式报错，而不是被静默吞掉。
    - validate_assignment：配置页逐字段修改时也会重新校验。
    - str_strip_whitespace：避免姓名、备注首尾空格造成“看似不同”的数据。
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )
