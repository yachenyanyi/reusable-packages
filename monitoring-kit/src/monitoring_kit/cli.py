"""只用于验证核心闭环的 CLI，不承载任何项目业务。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime

from .adapters.events.memory import InMemoryEventSink
from .adapters.persistence.memory import InMemoryHistoryStore, InMemoryRunStateStore
from .adapters.upstream.memory import InMemoryUpstreamGateway
from .collection.engine import CollectionEngine, RetryPolicy
from .collection.model import (
    AdapterContext,
    ObservedRecordDraft,
    UpstreamJobRequest,
    ValidationIssue,
    ValidationResult,
)
from .contracts import (
    ChangeQuery,
    ChangeKind,
    ExecutionContext,
    IngestKey,
    Observation,
    Presence,
    Provenance,
    RunRequest,
    RunStatus,
    SubjectRef,
    TypedEnvelope,
)
from .history.history import ContentHistory
from .history.model import ComparisonContext, RevisionDecision, RevisionMaterial
from .runtime import DeliveryGuarantee, DeliveryRetryPolicy, DispatchRequest, OutboxDispatcher
from .runtime.registry import ExtensionRegistry


class _DemoCollectionAdapter:
    adapter_key = "demo-collection"

    def supports(self, collection_type: str, schema_version: str) -> bool:
        return collection_type == "demo.collection" and schema_version == "1.0"

    def validate(self, spec: TypedEnvelope) -> ValidationResult:
        if not isinstance(spec.data.get("records"), list):
            return ValidationResult.invalid(ValidationIssue("records", "必须是数组"))
        return ValidationResult.ok()

    def build_upstream_request(self, spec: TypedEnvelope, context: AdapterContext) -> UpstreamJobRequest:
        return UpstreamJobRequest(collection=spec, source_ref=context.source_ref)

    def map_record(self, record, context: AdapterContext) -> ObservedRecordDraft:
        payload = dict(record.payload)
        key = str(payload.get("key") or record.external_id or record.record_id)
        content = None
        presence = Presence.ABSENT if record.deleted else Presence.PRESENT
        if presence is Presence.PRESENT:
            content = TypedEnvelope(
                "demo.content",
                "1.0",
                {"key": key, "body": str(payload.get("body", "")), "volatile": payload.get("volatile")},
            )
        return ObservedRecordDraft(
            gateway_key=record.job_ref.gateway_key,
            upstream_record_id=record.record_id,
            content_type_key="demo.content",
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
                collector_ref="cli-demo",
            ),
            published_at=record.published_at,
        )


class _DemoContentPolicy:
    content_type_key = "demo.content"
    subject_namespace = "demo.subject"
    policy_ref = "demo-content-policy@1.0"

    def supports(self, content_type_key: str, schema_version: str) -> bool:
        return content_type_key == self.content_type_key and schema_version == "1.0"

    def identify(self, draft: ObservedRecordDraft) -> SubjectRef:
        key = draft.identity_material.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("demo 记录缺少稳定 key")
        return SubjectRef(self.subject_namespace, key, "1.0")

    def prepare_revision(self, content: TypedEnvelope) -> RevisionMaterial:
        normalized = TypedEnvelope(
            self.content_type_key,
            "1.0",
            {"key": content.data["key"], "body": content.data.get("body", "")},
        )
        digest = hashlib.sha256(
            json.dumps(dict(normalized.data), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return RevisionMaterial(normalized, digest)

    def compare(
        self,
        previous_snapshot,
        current,
        material: RevisionMaterial | None,
        context: ComparisonContext,
    ) -> RevisionDecision:
        if current.presence is Presence.ABSENT:
            streak = context.consecutive_absences + 1
            kind = ChangeKind.MISSING_CONFIRMED if streak >= 2 else ChangeKind.MISSING_SUSPECTED
            return RevisionDecision(kind, False)
        if previous_snapshot is None or material is None:
            return RevisionDecision(ChangeKind.FIRST_SEEN, True)
        if context.document and context.document.state.value == "missing_confirmed":
            return RevisionDecision(
                ChangeKind.RESTORED,
                material.content_hash != previous_snapshot.content_hash,
            )
        if material.content_hash != previous_snapshot.content_hash:
            return RevisionDecision(ChangeKind.REVISED, True)
        return RevisionDecision(None, False)


def _demo_engine() -> CollectionEngine:
    now = datetime.now(UTC)

    def records(_request: UpstreamJobRequest):
        return [
            {"record_id": "demo-1", "external_id": "item-1", "payload": {"key": "item-1", "body": "第一版"}, "observed_at": now},
            {"record_id": "demo-2", "external_id": "item-1", "payload": {"key": "item-1", "body": "第二版", "volatile": 7}, "observed_at": now},
        ]

    registry = ExtensionRegistry()
    registry.register_collection_adapter(_DemoCollectionAdapter())
    registry.register_content_policy(_DemoContentPolicy())
    history_store = InMemoryHistoryStore()
    history = ContentHistory(history_store, registry, event_sink=InMemoryEventSink())
    gateway = InMemoryUpstreamGateway(records, page_size=1)
    return CollectionEngine(
        InMemoryRunStateStore(),
        history,
        registry,
        gateway,
        retry_policy=RetryPolicy(poll_interval_seconds=0, max_batches_per_wake=10),
    )


def run_demo() -> int:
    engine = _demo_engine()
    request = RunRequest(
        collection=TypedEnvelope("demo.collection", "1.0", {"records": ["demo"]}),
        source_ref="demo-source",
    )
    context = ExecutionContext("cli-demo", "cli", "demo-run-1")
    ref = engine.submit_run(request, context)
    for _ in range(5):
        summary = engine.get_run(ref.run_id, "cli-demo")
        if summary.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            break
        engine.wake()
    summary = engine.get_run(ref.run_id, "cli-demo")
    changes = engine.query_changes(ChangeQuery(limit=100), "cli-demo")
    print(json.dumps({"run": summary.to_dict(), "changes": [event.kind.value for event in changes.events]}, ensure_ascii=False, indent=2))
    return 0 if summary.status is RunStatus.COMPLETED else 1


class _DemoPublisher:
    def __init__(self) -> None:
        self.failures = 1
        self.events = []

    def publish(self, events) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("CLI 演示中的临时投递失败")
        self.events.extend(events)


def run_dispatch_demo() -> int:
    """用内存 Outbox 演示“失败保留、重试后投递”的独立运行方式。"""

    now = datetime.now(UTC)
    registry = ExtensionRegistry()
    registry.register_content_policy(_DemoContentPolicy())
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    history = ContentHistory(store, registry)
    result = history.record(
        Observation(
            observation_id="cli-demo-observation",
            scope_key="cli-demo",
            run_id="cli-demo-run",
            ingest_key=IngestKey("cli-demo-gateway", "cli-demo-record"),
            subject=SubjectRef("demo.subject", "item-1", "1.0"),
            observed_at=now,
            presence=Presence.PRESENT,
            content=TypedEnvelope("demo.content", "1.0", {"key": "item-1", "body": "demo"}),
            provenance=Provenance(source_ref="cli-demo"),
            received_at=now,
        )
    )
    publisher = _DemoPublisher()
    dispatcher = OutboxDispatcher(
        store,
        publisher,
        retry_policy=DeliveryRetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
    )
    first = dispatcher.dispatch_once(DispatchRequest("cli-worker", now))
    second = dispatcher.dispatch_once(DispatchRequest("cli-worker", now))
    print(
        json.dumps(
            {
                "event_id": result.events[0].event_id,
                "first_dispatch": {
                    "claimed": first.claimed,
                    "delivered": first.delivered,
                    "retried": first.retried,
                    "blocked": first.blocked,
                },
                "second_dispatch": {
                    "claimed": second.claimed,
                    "delivered": second.delivered,
                    "retried": second.retried,
                    "blocked": second.blocked,
                },
                "published_event_ids": [event.event_id for event in publisher.events],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if second.delivered == 1 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="monitoring-kit", description="monitoring-kit 核心库验收 CLI")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("demo", help="运行通用核心闭环演示")
    subparsers.add_parser("dispatch", help="运行 Outbox 可靠投递演示")
    args = parser.parse_args(argv)
    if args.command == "demo":
        return run_demo()
    if args.command == "dispatch":
        return run_dispatch_demo()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
