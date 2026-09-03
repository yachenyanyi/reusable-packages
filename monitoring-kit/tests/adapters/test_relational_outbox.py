from __future__ import annotations

from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence
from monitoring_kit.contracts import IngestKey, Observation, Presence, Provenance, SubjectRef, TypedEnvelope
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.runtime import (
    DeliveryGuarantee,
    DeliveryRetryPolicy,
    DispatchRequest,
    OutboxDispatcher,
)

from tests.support import ManualClock, TestCollectionAdapter, TestContentPolicy
from monitoring_kit.runtime.registry import ExtensionRegistry


def _registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    return registry


def _observation(clock: ManualClock, record_id: str, body: str) -> Observation:
    now = clock.now()
    return Observation(
        observation_id=f"observation-{record_id}",
        scope_key="scope-a",
        run_id=f"run-{record_id}",
        ingest_key=IngestKey("relational-gateway", record_id),
        subject=SubjectRef("test.content", "same", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("test.content", "1.0", {"key": "same", "body": body}),
        provenance=Provenance(),
        received_at=now,
    )


class _Publisher:
    def __init__(self) -> None:
        self.fail = True
        self.events = []

    def publish(self, events) -> None:
        if self.fail:
            self.fail = False
            raise RuntimeError("模拟传输失败")
        self.events.extend(events)


def test_sqlite_outbox_survives_publish_failure_and_reopens(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'relational-outbox.db').as_posix()}"
    bundle = open_persistence(
        PersistenceConfig(
            database_url,
            delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE,
        )
    )
    clock = ManualClock()
    publisher = _Publisher()
    dispatcher = OutboxDispatcher(
        bundle.event_delivery_store,
        publisher,
        retry_policy=DeliveryRetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=2),
    )
    try:
        history = ContentHistory(bundle.history_store, _registry(), clock=clock)
        result = history.record(_observation(clock, "one", "v1"))
        failed = dispatcher.dispatch_once(DispatchRequest("worker-a", clock.now()))
        assert (failed.claimed, failed.retried, failed.delivered) == (1, 1, 0)
        bundle.close()

        reopened = open_persistence(
            PersistenceConfig(
                database_url,
                delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE,
            )
        )
        try:
            clock.advance(2)
            publisher.fail = False
            delivered = OutboxDispatcher(
                reopened.event_delivery_store,
                publisher,
                retry_policy=DeliveryRetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=2),
            ).dispatch_once(DispatchRequest("worker-b", clock.now()))
            assert (delivered.claimed, delivered.delivered) == (1, 1)
            assert [event.event_id for event in publisher.events] == [result.events[0].event_id]
            assert reopened.event_delivery_store.claim_pending(
                DispatchRequest("worker-c", clock.now())
            ) == ()
        finally:
            reopened.close()
    finally:
        bundle.close()
