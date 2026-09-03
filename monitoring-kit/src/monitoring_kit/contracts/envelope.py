"""带命名类型和版本的扩展载荷。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .primitives import (
    json_clone,
    require_mapping,
    validate_schema_version,
    validate_type_key,
)


@dataclass(frozen=True, slots=True)
class TypedEnvelope:
    """扩展契约的稳定外壳。

    `data` 仍由具体扩展定义和验证；核心只保证它是可版本化的 JSON 对象，
    不把任意键值袋解释成业务模型。
    """

    type_key: str
    schema_version: str
    data: Mapping[str, Any]

    def __init__(self, type_key: str, schema_version: str, data: Any) -> None:
        object.__setattr__(self, "type_key", validate_type_key(type_key))
        object.__setattr__(self, "schema_version", validate_schema_version(schema_version))
        copied = require_mapping(data, "data")
        object.__setattr__(self, "data", MappingProxyType(copied))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type_key": self.type_key,
            "schema_version": self.schema_version,
            "data": json_clone(dict(self.data)),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TypedEnvelope":
        if not isinstance(value, dict):
            raise ValueError("TypedEnvelope 必须是对象")
        return cls(value["type_key"], value["schema_version"], value["data"])
