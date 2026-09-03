"""内容历史和变化事件的稳定契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .envelope import TypedEnvelope
from .observation import Provenance, SubjectRef
from .primitives import CORE_CONTRACT_VERSION, ensure_utc, parse_datetime, require_text, validate_contract_version


class ChangeKind(str, Enum):
    FIRST_SEEN = "first_seen"
    REVISED = "revised"
    MISSING_SUSPECTED = "missing_suspected"
    MISSING_CONFIRMED = "missing_confirmed"
    RESTORED = "restored"


class DocumentState(str, Enum):
    PRESENT = "present"
    MISSING_SUSPECTED = "missing_suspected"
    MISSING_CONFIRMED = "missing_confirmed"


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    scope_key: str
    subject: SubjectRef
    state: DocumentState
    current_snapshot_id: str | None
    first_observed_at: datetime
    last_observed_at: datetime
    missing_streak: int
    policy_ref: str

    def __post_init__(self) -> None:
        for field_name in ("document_id", "scope_key", "policy_ref"):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "state", DocumentState(self.state))
        object.__setattr__(self, "first_observed_at", ensure_utc(self.first_observed_at, "first_observed_at"))
        object.__setattr__(self, "last_observed_at", ensure_utc(self.last_observed_at, "last_observed_at"))
        if self.missing_streak < 0:
            raise ValueError("missing_streak 不能为负数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "scope_key": self.scope_key,
            "subject": self.subject.to_dict(),
            "state": self.state.value,
            "current_snapshot_id": self.current_snapshot_id,
            "first_observed_at": self.first_observed_at.isoformat(),
            "last_observed_at": self.last_observed_at.isoformat(),
            "missing_streak": self.missing_streak,
            "policy_ref": self.policy_ref,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Document":
        return cls(
            document_id=value["document_id"],
            scope_key=value["scope_key"],
            subject=SubjectRef.from_dict(value["subject"]),
            state=DocumentState(value["state"]),
            current_snapshot_id=value.get("current_snapshot_id"),
            first_observed_at=parse_datetime(value["first_observed_at"], "first_observed_at"),
            last_observed_at=parse_datetime(value["last_observed_at"], "last_observed_at"),
            missing_streak=value["missing_streak"],
            policy_ref=value["policy_ref"],
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    snapshot_id: str
    document_id: str
    scope_key: str
    revision: int
    observed_at: datetime
    recorded_at: datetime
    content: TypedEnvelope
    content_hash: str
    run_id: str
    observation_id: str
    provenance: Provenance

    def __post_init__(self) -> None:
        for field_name in (
            "snapshot_id",
            "document_id",
            "scope_key",
            "content_hash",
            "run_id",
            "observation_id",
        ):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        if not isinstance(self.content, TypedEnvelope):
            raise ValueError("Snapshot.content 必须是 TypedEnvelope")
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "recorded_at", ensure_utc(self.recorded_at, "recorded_at"))
        if self.revision < 1:
            raise ValueError("revision 必须从 1 开始")

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "document_id": self.document_id,
            "scope_key": self.scope_key,
            "revision": self.revision,
            "observed_at": self.observed_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "content": self.content.to_dict(),
            "content_hash": self.content_hash,
            "run_id": self.run_id,
            "observation_id": self.observation_id,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Snapshot":
        return cls(
            snapshot_id=value["snapshot_id"],
            document_id=value["document_id"],
            scope_key=value["scope_key"],
            revision=value["revision"],
            observed_at=parse_datetime(value["observed_at"], "observed_at"),
            recorded_at=parse_datetime(value["recorded_at"], "recorded_at"),
            content=TypedEnvelope.from_dict(value["content"]),
            content_hash=value["content_hash"],
            run_id=value["run_id"],
            observation_id=value["observation_id"],
            provenance=Provenance.from_dict(value.get("provenance", {})),
        )


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    event_id: str
    scope_key: str
    document_id: str
    run_id: str
    sequence: int
    kind: ChangeKind
    occurred_at: datetime
    effective_observed_at: datetime
    from_snapshot_id: str | None = None
    to_snapshot_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    policy_ref: str = ""
    details: TypedEnvelope | None = None
    contract_version: str = CORE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in ("event_id", "scope_key", "document_id", "run_id", "policy_ref", "contract_version"):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "contract_version", validate_contract_version(self.contract_version))
        object.__setattr__(self, "kind", ChangeKind(self.kind))
        if self.sequence < 1:
            raise ValueError("sequence 必须从 1 开始")
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(
            self,
            "effective_observed_at",
            ensure_utc(self.effective_observed_at, "effective_observed_at"),
        )
        object.__setattr__(self, "evidence_refs", tuple(require_text(item, "evidence_ref") for item in self.evidence_refs))
        if self.kind is ChangeKind.FIRST_SEEN and (self.from_snapshot_id or not self.to_snapshot_id):
            raise ValueError("FIRST_SEEN 必须只有 to_snapshot_id")
        if self.kind is ChangeKind.REVISED and (not self.from_snapshot_id or not self.to_snapshot_id):
            raise ValueError("REVISED 必须同时有前后 Snapshot")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "scope_key": self.scope_key,
            "document_id": self.document_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(),
            "effective_observed_at": self.effective_observed_at.isoformat(),
            "from_snapshot_id": self.from_snapshot_id,
            "to_snapshot_id": self.to_snapshot_id,
            "evidence_refs": list(self.evidence_refs),
            "policy_ref": self.policy_ref,
            "details": self.details.to_dict() if self.details else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChangeEvent":
        details = value.get("details")
        return cls(
            event_id=value["event_id"],
            scope_key=value["scope_key"],
            document_id=value["document_id"],
            run_id=value["run_id"],
            sequence=value["sequence"],
            kind=ChangeKind(value["kind"]),
            occurred_at=parse_datetime(value["occurred_at"], "occurred_at"),
            effective_observed_at=parse_datetime(value["effective_observed_at"], "effective_observed_at"),
            from_snapshot_id=value.get("from_snapshot_id"),
            to_snapshot_id=value.get("to_snapshot_id"),
            evidence_refs=tuple(value.get("evidence_refs", ())),
            policy_ref=value["policy_ref"],
            details=TypedEnvelope.from_dict(details) if details else None,
            contract_version=value.get("contract_version", CORE_CONTRACT_VERSION),
        )


@dataclass(frozen=True, slots=True)
class ChangeQuery:
    cursor: str | None = None
    limit: int = 100
    document_id: str | None = None
    kinds: frozenset[ChangeKind] = field(default_factory=frozenset)
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        if self.document_id is not None:
            object.__setattr__(self, "document_id", require_text(self.document_id, "document_id"))
        if self.occurred_after is not None:
            object.__setattr__(self, "occurred_after", ensure_utc(self.occurred_after, "occurred_after"))
        if self.occurred_before is not None:
            object.__setattr__(self, "occurred_before", ensure_utc(self.occurred_before, "occurred_before"))
        if self.occurred_after and self.occurred_before and self.occurred_before <= self.occurred_after:
            raise ValueError("occurred_before 必须晚于 occurred_after")
        object.__setattr__(self, "kinds", frozenset(ChangeKind(kind) for kind in self.kinds))


@dataclass(frozen=True, slots=True)
class ChangePage:
    events: tuple[ChangeEvent, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class DocumentView:
    document: Document
    current_snapshot: Snapshot | None


@dataclass(frozen=True, slots=True)
class SnapshotTimeline:
    document: Document
    snapshots: tuple[Snapshot, ...]
    events: tuple[ChangeEvent, ...]
