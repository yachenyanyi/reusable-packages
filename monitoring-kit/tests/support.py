from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from monitoring_kit.adapters.events.memory import InMemoryAuditSink, InMemoryEventSink, InMemoryTelemetrySink
from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore, InMemoryRunStateStore
from monitoring_kit.collection.engine import CollectionEngine, RetryPolicy
from monitoring_kit.collection.model import (
    AdapterContext,
    ObservedRecordDraft,
    UpstreamBatch,
    UpstreamCancellation,
    UpstreamJobRef,
    UpstreamJobRequest,
    UpstreamRecord,
    UpstreamState,
    UpstreamStatus,
    ValidationIssue,
    ValidationResult,
)
from monitoring_kit.collection.ports import UpstreamError
from monitoring_kit.contracts import (
    ChangeKind,
    DocumentState,
    ExecutionContext,
    Presence,
    Provenance,
    RunRequest,
    SubjectRef,
    TypedEnvelope,
)
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.history.model import ComparisonContext, RevisionDecision, RevisionMaterial
from monitoring_kit.runtime.registry import ExtensionRegistry
from monitoring_kit.contracts.primitives import stable_json


class ManualClock:
    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class TestCollectionAdapter:
    __test__ = False
    adapter_key = "test-collection-adapter"

    def supports(self, collection_type: str, schema_version: str) -> bool:
        return collection_type == "test.collection" and schema_version == "1.0"

    def validate(self, spec: TypedEnvelope) -> ValidationResult:
        if spec.data.get("invalid"):
            return ValidationResult.invalid(ValidationIssue("invalid", "测试请求被标记为无效"))
        if not isinstance(spec.data.get("records", []), list):
            return ValidationResult.invalid(ValidationIssue("records", "必须是数组"))
        return ValidationResult.ok()

    def build_upstream_request(self, spec: TypedEnvelope, context: AdapterContext) -> UpstreamJobRequest:
        return UpstreamJobRequest(
            collection=spec,
            source_ref=context.source_ref,
            gateway_hint=spec.data.get("gateway_hint"),
        )

    def map_record(self, record: UpstreamRecord, context: AdapterContext) -> ObservedRecordDraft:
        payload = dict(record.payload)
        key = str(payload.get("key") or record.external_id or record.record_id)
        presence = Presence.ABSENT if record.deleted else Presence.PRESENT
        content = None
        if presence is Presence.PRESENT:
            content = TypedEnvelope(
                "test.content",
                "1.0",
                {
                    "key": key,
                    "body": str(payload.get("body", "")),
                    "volatile": payload.get("volatile"),
                },
            )
        return ObservedRecordDraft(
            gateway_key=record.job_ref.gateway_key,
            upstream_record_id=record.record_id,
            content_type_key="test.content",
            content_schema_version="1.0",
            observed_at=record.observed_at,
            presence=presence,
            content=content,
            identity_material={"key": key},
            provenance=Provenance(
                source_ref=context.source_ref,
                upstream_job_ref=context.upstream_job_ref,
                upstream_external_id=record.external_id,
                raw_artifact_ref=record.raw_artifact_ref,
                collector_ref="tests",
            ),
            published_at=record.published_at,
        )


class TestContentPolicy:
    __test__ = False
    content_type_key = "test.content"
    subject_namespace = "test.content"
    policy_ref = "test-content-policy@1.0"

    def __init__(self, missing_threshold: int = 2) -> None:
        self.missing_threshold = missing_threshold

    def supports(self, content_type_key: str, schema_version: str) -> bool:
        return content_type_key == self.content_type_key and schema_version == "1.0"

    def identify(self, draft: ObservedRecordDraft) -> SubjectRef:
        key = draft.identity_material.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("缺少稳定 identity key")
        return SubjectRef(self.content_type_key, key, "1.0")

    def prepare_revision(self, content: TypedEnvelope) -> RevisionMaterial:
        normalized = TypedEnvelope(
            self.content_type_key,
            "1.0",
            {"key": content.data["key"], "body": content.data.get("body", "")},
        )
        content_hash = hashlib.sha256(stable_json(normalized.to_dict()).encode("utf-8")).hexdigest()
        return RevisionMaterial(normalized, content_hash)

    def compare(
        self,
        previous_snapshot,
        current,
        material: RevisionMaterial | None,
        context: ComparisonContext,
    ) -> RevisionDecision:
        if current.presence is Presence.ABSENT:
            streak = context.consecutive_absences + 1
            kind = ChangeKind.MISSING_CONFIRMED if streak >= self.missing_threshold else ChangeKind.MISSING_SUSPECTED
            return RevisionDecision(kind, False)
        if previous_snapshot is None or material is None:
            return RevisionDecision(ChangeKind.FIRST_SEEN, True)
        if context.document and context.document.state is DocumentState.MISSING_CONFIRMED:
            return RevisionDecision(
                ChangeKind.RESTORED,
                material.content_hash != previous_snapshot.content_hash,
            )
        if material.content_hash != previous_snapshot.content_hash:
            return RevisionDecision(ChangeKind.REVISED, True)
        return RevisionDecision(None, False)


class ScriptedGateway:
    gateway_key = "scripted"

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        page_size: int = 100,
        submit_failures: list[UpstreamError] | None = None,
        status_failures: list[UpstreamError] | None = None,
        replay_first_page: bool = False,
        gateway_key: str = "scripted",
    ) -> None:
        self.records_data = records
        self.page_size = page_size
        self.submit_failures = list(submit_failures or [])
        self.status_failures = list(status_failures or [])
        self.replay_first_page = replay_first_page
        self.gateway_key = gateway_key
        self.submit_calls = 0
        self.status_calls = 0
        self.read_calls = 0
        self.cancel_calls = 0
        self._jobs: dict[str, tuple[UpstreamJobRef, tuple[UpstreamRecord, ...]]] = {}
        self._idempotency: dict[str, UpstreamJobRef] = {}

    def submit(self, request: UpstreamJobRequest, idempotency_key: str) -> UpstreamJobRef:
        self.submit_calls += 1
        if self.submit_failures:
            raise self.submit_failures.pop(0)
        if idempotency_key in self._idempotency:
            return self._idempotency[idempotency_key]
        ref = UpstreamJobRef(self.gateway_key, f"job-{len(self._jobs) + 1}")
        now = datetime(2026, 1, 1, tzinfo=UTC)
        records = tuple(
            UpstreamRecord(
                record_id=str(item.get("record_id", index)),
                job_ref=ref,
                source_type=request.collection.type_key,
                schema_version=request.collection.schema_version,
                observed_at=item.get("observed_at", now + timedelta(seconds=index)),
                payload=item.get("payload", {}),
                external_id=item.get("external_id"),
                deleted=bool(item.get("deleted", False)),
                raw_artifact_ref=item.get("raw_artifact_ref"),
                provenance=item.get("provenance", {}),
                sequence=index,
            )
            for index, item in enumerate(self.records_data, start=1)
        )
        self._jobs[ref.job_id] = (ref, records)
        self._idempotency[idempotency_key] = ref
        return ref

    def get_status(self, job_ref: UpstreamJobRef) -> UpstreamStatus:
        self.status_calls += 1
        if self.status_failures:
            raise self.status_failures.pop(0)
        records = self._jobs[job_ref.job_id][1]
        return UpstreamStatus(
            job_ref=job_ref,
            state=UpstreamState.COMPLETED,
            produced_count=len(records),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def read_batch(self, job_ref: UpstreamJobRef, cursor: str | None) -> UpstreamBatch:
        self.read_calls += 1
        records = self._jobs[job_ref.job_id][1]
        offset = int(cursor) if cursor else 0
        if self.replay_first_page and offset == 1:
            offset = 0
        end = min(offset + self.page_size, len(records))
        has_more = end < len(records)
        return UpstreamBatch(records[offset:end], str(end) if has_more else None, has_more, "1.0")

    def cancel(self, job_ref: UpstreamJobRef) -> UpstreamCancellation:
        self.cancel_calls += 1
        return UpstreamCancellation(True, UpstreamState.CANCELLED)


def build_engine(
    records: list[dict[str, Any]],
    *,
    clock: ManualClock | None = None,
    gateway: ScriptedGateway | None = None,
    retry_policy: RetryPolicy | None = None,
    audit_sink: InMemoryAuditSink | None = None,
    telemetry_sink: InMemoryTelemetrySink | None = None,
):
    clock = clock or ManualClock()
    gateway = gateway or ScriptedGateway(records)
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    history_store = InMemoryHistoryStore()
    event_sink = InMemoryEventSink()
    history = ContentHistory(history_store, registry, event_sink=event_sink, clock=clock)
    state_store = InMemoryRunStateStore()
    engine = CollectionEngine(
        state_store,
        history,
        registry,
        gateway,
        clock=clock,
        retry_policy=retry_policy or RetryPolicy(poll_interval_seconds=0, lease_seconds=30),
        worker_id="test-worker",
        audit_sink=audit_sink,
        telemetry_sink=telemetry_sink,
    )
    return engine, state_store, history_store, event_sink, gateway, clock


def request(name: str = "one", *, gateway_hint: str | None = None) -> RunRequest:
    data = {"records": [name]}
    if gateway_hint is not None:
        data["gateway_hint"] = gateway_hint
    return RunRequest(
        collection=TypedEnvelope(
            "test.collection",
            "1.0",
            data,
        ),
        source_ref="test-source",
    )


def context(key: str = "run-1", scope: str = "scope-a") -> ExecutionContext:
    return ExecutionContext(scope, "test-actor", key)
