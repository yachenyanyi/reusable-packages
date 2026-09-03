"""内容历史模块拥有的持久化与事件端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..contracts.change import (
    ChangeEvent,
    ChangePage,
    ChangeQuery,
    Document,
    DocumentView,
    Snapshot,
    SnapshotTimeline,
)
from ..contracts.observation import IngestKey, SubjectRef
from .model import ComparisonContext, HistoryResult, HistoryWrite, RevisionDecision, RevisionMaterial


class HistoryStore(Protocol):
    def get_by_ingest_key(self, scope_key: str, ingest_key: IngestKey):
        ...

    def get_document(self, scope_key: str, subject: SubjectRef):
        ...

    def commit(self, write: HistoryWrite) -> None:
        ...

    def get_current(self, scope_key: str, document_id: str) -> DocumentView | None:
        ...

    def get_timeline(self, scope_key: str, document_id: str) -> SnapshotTimeline | None:
        ...

    def query_changes(self, scope_key: str, query: ChangeQuery) -> ChangePage:
        ...


class EventSink(Protocol):
    """变化事实提交后的至少一次投递端口。"""

    def publish(self, events: Sequence[ChangeEvent]) -> None:
        ...


class ContentPolicy(Protocol):
    """由内容类型拥有身份、规范化和变化解释规则。"""

    policy_ref: str
    content_type_key: str
    subject_namespace: str

    def supports(self, content_type_key: str, schema_version: str) -> bool:
        ...

    def identify(self, draft) -> SubjectRef:
        ...

    def prepare_revision(self, content: TypedEnvelope) -> RevisionMaterial:
        ...

    def compare(
        self,
        previous_snapshot,
        current,
        material: RevisionMaterial | None,
        context: ComparisonContext,
    ) -> RevisionDecision:
        ...
