"""核心运行事实的审计契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .envelope import TypedEnvelope
from .primitives import CORE_CONTRACT_VERSION, ensure_utc, parse_datetime, require_text, validate_contract_version


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    scope_key: str
    action: str
    object_type: str
    object_id: str
    occurred_at: datetime
    outcome: str
    actor_ref: str | None = None
    trace_ref: str | None = None
    details: TypedEnvelope | None = None
    contract_version: str = CORE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "scope_key",
            "action",
            "object_type",
            "object_id",
            "outcome",
            "contract_version",
        ):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "contract_version", validate_contract_version(self.contract_version))
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "occurred_at"))
        if self.actor_ref is not None:
            object.__setattr__(self, "actor_ref", require_text(self.actor_ref, "actor_ref"))
        if self.trace_ref is not None:
            object.__setattr__(self, "trace_ref", require_text(self.trace_ref, "trace_ref"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "scope_key": self.scope_key,
            "action": self.action,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "occurred_at": self.occurred_at.isoformat(),
            "outcome": self.outcome,
            "actor_ref": self.actor_ref,
            "trace_ref": self.trace_ref,
            "details": self.details.to_dict() if self.details else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuditEvent":
        details = value.get("details")
        return cls(
            event_id=value["event_id"],
            scope_key=value["scope_key"],
            action=value["action"],
            object_type=value["object_type"],
            object_id=value["object_id"],
            occurred_at=parse_datetime(value["occurred_at"], "occurred_at"),
            outcome=value["outcome"],
            actor_ref=value.get("actor_ref"),
            trace_ref=value.get("trace_ref"),
            details=TypedEnvelope.from_dict(details) if details else None,
            contract_version=value.get("contract_version", CORE_CONTRACT_VERSION),
        )
