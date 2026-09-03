"""内容历史模块的内部写入结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..contracts.change import ChangeEvent, ChangeKind, Document, Snapshot
from ..contracts.envelope import TypedEnvelope
from ..contracts.observation import Observation
from ..contracts.primitives import require_text


class HistoryCommitOutcome(str, Enum):
    """一次 HistoryWrite 对持久化状态的实际结果。"""

    CREATED = "created"
    DUPLICATE = "duplicate"


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
    base_document: Document | None

    def __post_init__(self) -> None:
        if len(self.ingest_key) != 2 or not all(isinstance(item, str) and item for item in self.ingest_key):
            raise ValueError("HistoryWrite.ingest_key 必须是非空二元组")
        object.__setattr__(
            self,
            "observation_fingerprint",
            require_text(self.observation_fingerprint, "observation_fingerprint"),
        )
        if not isinstance(self.result, HistoryResult):
            raise ValueError("HistoryWrite.result 必须是 HistoryResult")

        observation = self.result.observation
        document = self.result.document
        if self.ingest_key != (
            observation.ingest_key.gateway_key,
            observation.ingest_key.upstream_record_id,
        ):
            raise ValueError("HistoryWrite.ingest_key 与 Observation 不一致")
        if observation.scope_key != document.scope_key or observation.subject != document.subject:
            raise ValueError("Observation 与 Document 的身份或 scope 不一致")
        if self.base_document is not None:
            if (
                self.base_document.scope_key != document.scope_key
                or self.base_document.subject != document.subject
                or self.base_document.document_id != document.document_id
            ):
                raise ValueError("HistoryWrite.base_document 与 Document 不一致")
        snapshot = self.result.snapshot
        if snapshot is not None and (
            snapshot.scope_key != document.scope_key
            or snapshot.document_id != document.document_id
        ):
            raise ValueError("Snapshot 与 Document 不一致")
        for event in self.result.events:
            if event.scope_key != document.scope_key or event.document_id != document.document_id:
                raise ValueError("ChangeEvent 与 Document 不一致")


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
