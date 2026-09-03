"""采集模块拥有的端口和扩展 SPI。"""

from __future__ import annotations

from typing import Protocol

from ..contracts.envelope import TypedEnvelope
from ..runtime.model import AllocationRequest
from .model import (
    AdapterContext,
    ObservedRecordDraft,
    UpstreamBatch,
    UpstreamCancellation,
    UpstreamJobRef,
    UpstreamJobRequest,
    UpstreamStatus,
    ValidationResult,
)


class UpstreamError(Exception):
    """网关向采集引擎报告的可分类上游失败，不向宿主泄漏。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        fallback_allowed: bool = False,
        submission_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.fallback_allowed = fallback_allowed
        self.submission_unknown = submission_unknown


class RunStateConflictError(Exception):
    """Run 的本地副本已过期，调用方必须放弃该副本。"""


class UpstreamJobGateway(Protocol):
    """统一采集任务的最小协议；HTTP 和供应商细节留在实现侧。"""

    gateway_key: str

    def submit(self, request: UpstreamJobRequest, idempotency_key: str) -> UpstreamJobRef:
        ...

    def get_status(self, job_ref: UpstreamJobRef) -> UpstreamStatus:
        ...

    def read_batch(self, job_ref: UpstreamJobRef, cursor: str | None) -> UpstreamBatch:
        ...

    def cancel(self, job_ref: UpstreamJobRef) -> UpstreamCancellation:
        ...


class CollectionAdapter(Protocol):
    """把一个场景规格完整翻译成上游任务和观察草稿。"""

    adapter_key: str

    def supports(self, collection_type: str, schema_version: str) -> bool:
        ...

    def validate(self, spec: TypedEnvelope) -> ValidationResult:
        ...

    def build_upstream_request(self, spec: TypedEnvelope, context: AdapterContext) -> UpstreamJobRequest:
        ...

    def map_record(self, record: object, context: AdapterContext) -> ObservedRecordDraft:
        ...


class RunStateStore(Protocol):
    """运行模块真正需要的状态能力，不暴露数据库 CRUD。"""

    def find_by_idempotency(self, scope_key: str, idempotency_key: str):
        ...

    def create(self, record) -> None:
        ...

    def get(self, scope_key: str, run_id: str):
        ...

    def save(self, record) -> None:
        ...

    def allocate(self, request: AllocationRequest):
        ...

    def list_incomplete(self):
        ...


class ArtifactStore(Protocol):
    """可选的不可变原始证据存储端口。"""

    def put(self, content: bytes, *, media_type: str, metadata: dict[str, str] | None = None) -> str:
        ...

    def get(self, artifact_ref: str) -> bytes:
        ...
