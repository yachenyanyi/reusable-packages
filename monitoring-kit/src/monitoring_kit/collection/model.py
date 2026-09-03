"""采集模块内部模型和适配器之间的稳定 SPI 类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..contracts.envelope import TypedEnvelope
from ..contracts.observation import (
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
)
from ..contracts.primitives import (
    ensure_utc,
    new_id,
    require_mapping,
    require_text,
    validate_schema_version,
    validate_type_key,
)
from ..contracts.run import ExecutionContext, RunError, RunRequest, RunStatus, RunSummary


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    path: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", require_text(self.path, "path"))
        object.__setattr__(self, "message", require_text(self.message, "message"))


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        if self.valid and self.issues:
            raise ValueError("valid 的 ValidationResult 不能包含 issues")
        if not self.valid and not self.issues:
            raise ValueError("无效的 ValidationResult 必须包含 issues")

    @classmethod
    def ok(cls) -> "ValidationResult":
        return cls(True)

    @classmethod
    def invalid(cls, *issues: ValidationIssue) -> "ValidationResult":
        return cls(False, tuple(issues))


@dataclass(frozen=True, slots=True)
class AdapterContext:
    scope_key: str
    run_id: str
    source_ref: str | None
    upstream_job_ref: str | None = None
    attempt_ref: str | None = None


@dataclass(frozen=True, slots=True)
class UpstreamJobRequest:
    """不含 HTTP 细节的上游任务请求。"""

    collection: TypedEnvelope
    source_ref: str | None = None
    client_metadata: dict[str, Any] = field(default_factory=dict)
    gateway_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_metadata", require_mapping(self.client_metadata, "client_metadata"))
        if self.source_ref is not None:
            object.__setattr__(self, "source_ref", require_text(self.source_ref, "source_ref"))
        if self.gateway_hint is not None:
            object.__setattr__(
                self,
                "gateway_hint",
                require_text(self.gateway_hint, "gateway_hint", max_length=255),
            )


@dataclass(frozen=True, slots=True)
class UpstreamJobRef:
    gateway_key: str
    job_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gateway_key",
            require_text(self.gateway_key, "gateway_key", max_length=255),
        )
        object.__setattr__(self, "job_id", require_text(self.job_id, "job_id"))


class UpstreamState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class UpstreamErrorInfo:
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None
    fallback_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_text(self.code, "upstream_error.code"))
        object.__setattr__(self, "message", require_text(self.message, "upstream_error.message"))
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds 不能为负数")


@dataclass(frozen=True, slots=True)
class UpstreamStatus:
    job_ref: UpstreamJobRef
    state: UpstreamState
    produced_count: int = 0
    failed_count: int = 0
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    error: UpstreamErrorInfo | None = None
    latest_cursor: str | None = None

    def __post_init__(self) -> None:
        if self.produced_count < 0 or self.failed_count < 0:
            raise ValueError("上游计数不能为负数")
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", ensure_utc(self.updated_at, "updated_at"))
        if self.finished_at is not None:
            object.__setattr__(self, "finished_at", ensure_utc(self.finished_at, "finished_at"))
        if self.state in (UpstreamState.FAILED, UpstreamState.COMPLETED_WITH_ERRORS) and self.error is None:
            # 上游可以只提供 failed_count；核心不强迫供应商伪造错误文本。
            return


@dataclass(frozen=True, slots=True)
class UpstreamRecord:
    record_id: str
    job_ref: UpstreamJobRef
    source_type: str
    schema_version: str
    observed_at: datetime
    payload: dict[str, Any]
    external_id: str | None = None
    published_at: datetime | None = None
    deleted: bool = False
    raw_artifact_ref: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    sequence: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("record_id", "source_type"):
            object.__setattr__(self, field_name, require_text(getattr(self, field_name), field_name))
        object.__setattr__(self, "schema_version", validate_schema_version(self.schema_version))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", ensure_utc(self.published_at, "published_at"))
        object.__setattr__(self, "payload", require_mapping(self.payload, "payload"))
        object.__setattr__(self, "provenance", require_mapping(self.provenance, "provenance"))
        if self.external_id is not None:
            object.__setattr__(self, "external_id", require_text(self.external_id, "external_id"))
        if self.raw_artifact_ref is not None:
            object.__setattr__(self, "raw_artifact_ref", require_text(self.raw_artifact_ref, "raw_artifact_ref"))
        if not isinstance(self.deleted, bool):
            raise ValueError("deleted 必须是布尔值")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence 不能为负数")


@dataclass(frozen=True, slots=True)
class UpstreamBatch:
    records: tuple[UpstreamRecord, ...]
    next_cursor: str | None
    has_more: bool
    result_schema_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "result_schema_version", validate_schema_version(self.result_schema_version))
        if self.has_more and not self.next_cursor:
            raise ValueError("has_more=True 时必须提供 next_cursor")


@dataclass(frozen=True, slots=True)
class UpstreamCancellation:
    accepted: bool
    state: UpstreamState


@dataclass(frozen=True, slots=True)
class ObservedRecordDraft:
    """场景适配器产出的内部草稿；最终 subject 仍由 ContentPolicy 生成。"""

    gateway_key: str
    upstream_record_id: str
    content_type_key: str
    content_schema_version: str
    observed_at: datetime
    presence: Presence
    content: TypedEnvelope | None
    identity_material: dict[str, Any]
    provenance: Provenance
    published_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gateway_key",
            require_text(self.gateway_key, "gateway_key", max_length=255),
        )
        object.__setattr__(self, "upstream_record_id", require_text(self.upstream_record_id, "upstream_record_id"))
        object.__setattr__(self, "content_type_key", validate_type_key(self.content_type_key, "content_type_key"))
        object.__setattr__(self, "content_schema_version", validate_schema_version(self.content_schema_version))
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "identity_material", require_mapping(self.identity_material, "identity_material"))
        if self.published_at is not None:
            object.__setattr__(self, "published_at", ensure_utc(self.published_at, "published_at"))
        if self.presence is Presence.PRESENT:
            if self.content is None:
                raise ValueError("PRESENT 草稿必须有 content")
            if self.content.type_key != self.content_type_key or self.content.schema_version != self.content_schema_version:
                raise ValueError("草稿 content 与 content_type 不一致")
        elif self.content is not None:
            raise ValueError("ABSENT 草稿不能有 content")


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    run_id: str
    operation: str
    gateway_key: str | None
    started_at: datetime
    finished_at: datetime
    outcome: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RecordFailure:
    upstream_record_id: str
    code: str
    message: str


@dataclass
class RunRecord:
    run_id: str
    scope_key: str
    request: RunRequest
    context: ExecutionContext
    request_fingerprint: str
    adapter_key: str
    upstream_request: UpstreamJobRequest
    status: RunStatus
    accepted_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    upstream_job: UpstreamJobRef | None = None
    cursor: str | None = None
    processed_count: int = 0
    failed_count: int = 0
    change_count: int = 0
    consecutive_failures: int = 0
    next_wakeup_at: datetime | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    cancel_requested: bool = False
    error: RunError | None = None
    attempts: list[Attempt] = field(default_factory=list)
    processed_ingest_keys: set[tuple[str, str]] = field(default_factory=set)
    record_failures: list[RecordFailure] = field(default_factory=list)
    state_version: int = field(default=0, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    def summary(self) -> RunSummary:
        return RunSummary(
            run_id=self.run_id,
            scope_key=self.scope_key,
            status=self.status,
            processed_count=self.processed_count,
            failed_count=self.failed_count,
            change_count=self.change_count,
            attempt_count=len(self.attempts),
            accepted_at=self.accepted_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
        )


def observation_from_draft(
    draft: ObservedRecordDraft,
    *,
    scope_key: str,
    run_id: str,
    subject: SubjectRef,
    received_at: datetime,
) -> Observation:
    return Observation(
        observation_id=new_id(),
        scope_key=scope_key,
        run_id=run_id,
        ingest_key=IngestKey(draft.gateway_key, draft.upstream_record_id),
        subject=subject,
        observed_at=draft.observed_at,
        presence=draft.presence,
        content=draft.content,
        provenance=draft.provenance,
        published_at=draft.published_at,
        received_at=received_at,
    )
