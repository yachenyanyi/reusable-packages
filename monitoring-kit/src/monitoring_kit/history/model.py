"""内容历史模块的内部写入结果。"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.change import ChangeEvent, ChangeKind, Document, Snapshot
from ..contracts.envelope import TypedEnvelope
from ..contracts.observation import Observation


@dataclass(frozen=True, slots=True)
class HistoryResult:
    observation: Observation
    document: Document
    snapshot: Snapshot | None
    events: tuple[ChangeEvent, ...]
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class HistoryWrite:
    ingest_key: tuple[str, str]
    observation_fingerprint: str
    result: HistoryResult


@dataclass(frozen=True, slots=True)
class RevisionMaterial:
    normalized_content: TypedEnvelope
    content_hash: str


@dataclass(frozen=True, slots=True)
class ComparisonContext:
    document: Document | None
    previous_snapshot: Snapshot | None
    consecutive_absences: int


@dataclass(frozen=True, slots=True)
class RevisionDecision:
    kind: ChangeKind | None
    create_snapshot: bool
    details: TypedEnvelope | None = None
