"""跨模块稳定的数据契约。"""

from .change import (
    ChangeEvent,
    ChangeKind,
    ChangePage,
    ChangeQuery,
    Document,
    DocumentState,
    DocumentView,
    Snapshot,
    SnapshotTimeline,
)
from .audit import AuditEvent
from .envelope import TypedEnvelope
from .observation import (
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
)
from .run import (
    CancellationResult,
    ExecutionContext,
    ExecutionLimits,
    RecoverySummary,
    RequestedWindow,
    RunError,
    RunRef,
    RunRequest,
    RunStatus,
    RunSummary,
    RuntimePolicy,
    WorkSummary,
)

__all__ = [
    "AuditEvent",
    "CancellationResult",
    "ChangeEvent",
    "ChangeKind",
    "ChangePage",
    "ChangeQuery",
    "Document",
    "DocumentState",
    "DocumentView",
    "ExecutionContext",
    "ExecutionLimits",
    "IngestKey",
    "Observation",
    "Presence",
    "Provenance",
    "RecoverySummary",
    "RequestedWindow",
    "RunError",
    "RunRef",
    "RunRequest",
    "RunStatus",
    "RunSummary",
    "RuntimePolicy",
    "Snapshot",
    "SnapshotTimeline",
    "SubjectRef",
    "TypedEnvelope",
    "WorkSummary",
]
