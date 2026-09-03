"""确定性的内存上游网关，仅用于测试和本地演示。"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from ...collection.model import (
    UpstreamBatch,
    UpstreamCancellation,
    UpstreamJobRef,
    UpstreamJobRequest,
    UpstreamRecord,
    UpstreamState,
    UpstreamStatus,
)
from ...collection.ports import UpstreamJobGateway
from ...contracts.primitives import parse_datetime


class InMemoryUpstreamGateway(UpstreamJobGateway):
    """把请求转换成预先定义的结果，行为接近任务型统一 API。"""

    def __init__(
        self,
        records_factory: Callable[[UpstreamJobRequest], Iterable[dict[str, Any]]],
        *,
        gateway_key: str = "memory-upstream",
        page_size: int = 100,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size 必须大于 0")
        self.gateway_key = gateway_key
        self._records_factory = records_factory
        self._page_size = page_size
        self._jobs: dict[str, tuple[UpstreamJobRef, tuple[UpstreamRecord, ...]]] = {}
        self._idempotency: dict[str, UpstreamJobRef] = {}
        self._lock = threading.RLock()

    def submit(self, request: UpstreamJobRequest, idempotency_key: str) -> UpstreamJobRef:
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            if existing is not None:
                return existing
            ref = UpstreamJobRef(self.gateway_key, f"job_{uuid.uuid4()}")
            records = tuple(
                _make_record(item, request, ref, index)
                for index, item in enumerate(self._records_factory(request), start=1)
            )
            self._jobs[ref.job_id] = (ref, records)
            self._idempotency[idempotency_key] = ref
            return ref

    def get_status(self, job_ref: UpstreamJobRef) -> UpstreamStatus:
        records = self._job(job_ref)[1]
        return UpstreamStatus(
            job_ref=job_ref,
            state=UpstreamState.COMPLETED,
            produced_count=len(records),
            updated_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    def read_batch(self, job_ref: UpstreamJobRef, cursor: str | None) -> UpstreamBatch:
        records = self._job(job_ref)[1]
        offset = int(cursor) if cursor else 0
        if offset < 0 or offset > len(records):
            raise ValueError("无效的内存网关游标")
        end = min(offset + self._page_size, len(records))
        has_more = end < len(records)
        return UpstreamBatch(
            records=records[offset:end],
            next_cursor=str(end) if has_more else None,
            has_more=has_more,
            result_schema_version="1.0",
        )

    def cancel(self, job_ref: UpstreamJobRef) -> UpstreamCancellation:
        self._job(job_ref)
        return UpstreamCancellation(accepted=True, state=UpstreamState.CANCELLED)

    def _job(self, job_ref: UpstreamJobRef):
        with self._lock:
            try:
                return self._jobs[job_ref.job_id]
            except KeyError as exc:
                raise ValueError(f"未知内存上游任务: {job_ref.job_id}") from exc


def _make_record(
    value: dict[str, Any],
    request: UpstreamJobRequest,
    job_ref: UpstreamJobRef,
    sequence: int,
) -> UpstreamRecord:
    if not isinstance(value, dict):
        raise ValueError("内存网关的结果工厂必须返回对象")
    observed_at = value.get("observed_at", datetime.now(UTC))
    if isinstance(observed_at, str):
        observed_at = parse_datetime(observed_at, "observed_at")
    published_at = value.get("published_at")
    if isinstance(published_at, str):
        published_at = parse_datetime(published_at, "published_at")
    deleted = value.get("deleted", False)
    if not isinstance(deleted, bool):
        raise ValueError("内存网关的 deleted 必须是布尔值")
    return UpstreamRecord(
        record_id=str(value.get("record_id", f"record-{sequence}")),
        job_ref=job_ref,
        source_type=request.collection.type_key,
        schema_version=request.collection.schema_version,
        observed_at=observed_at,
        payload=value.get("payload", {}),
        external_id=value.get("external_id"),
        published_at=published_at,
        deleted=deleted,
        raw_artifact_ref=value.get("raw_artifact_ref"),
        provenance=value.get("provenance", {}),
        sequence=sequence,
    )
