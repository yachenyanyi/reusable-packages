"""稳定契约使用的基础校验，不承载业务逻辑。"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


CORE_CONTRACT_VERSION = "1.0"
_TYPE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_SCHEMA_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-+][A-Za-z0-9.-]+)?$")


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须带时区")
    return value.astimezone(UTC)


def parse_datetime(value: str | datetime, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field_name)
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 ISO 时间或 datetime")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return ensure_utc(datetime.fromisoformat(normalized), field_name)
    except ValueError as exc:
        raise ValueError(f"{field_name} 不是有效的 ISO 时间") from exc


def require_text(value: str, field_name: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field_name} 长度不能超过 {max_length} 个字符")
    return value


def validate_type_key(value: str, field_name: str = "type_key") -> str:
    value = require_text(value, field_name)
    if not _TYPE_KEY_RE.fullmatch(value):
        raise ValueError(f"{field_name} 含有不支持的字符")
    return value


def validate_schema_version(value: str) -> str:
    value = require_text(value, "schema_version")
    if not _SCHEMA_VERSION_RE.fullmatch(value):
        raise ValueError("schema_version 必须类似 1.0 或 1.0.1")
    return value


def validate_contract_version(value: str) -> str:
    value = require_text(value, "contract_version")
    if value != CORE_CONTRACT_VERSION:
        raise ValueError(f"当前仅支持 contract_version={CORE_CONTRACT_VERSION}")
    return value


def json_clone(value: Any) -> Any:
    """验证并复制 JSON 形状，防止契约保存调用方的可变引用。"""

    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("契约中的扩展数据必须是 JSON 可编码值") from exc


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def require_mapping(value: Mapping[str, Any], field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是对象")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise ValueError(f"{field_name} 的键必须是非空字符串")
    cloned = json_clone(dict(value))
    if not isinstance(cloned, dict):  # pragma: no cover - json_clone 已保证
        raise ValueError(f"{field_name} 必须是对象")
    return cloned
