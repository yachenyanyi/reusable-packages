from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence
from monitoring_kit.collection.engine import CollectionEngine
from monitoring_kit.collection.ports import RunStateConflictError
from monitoring_kit.contracts import (
    ExecutionContext,
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
    TypedEnvelope,
)
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.history.model import HistoryWrite
from monitoring_kit.adapters.persistence.relational import PersistenceInvariantError
from monitoring_kit.runtime import (
    AllocationRequest,
    DeliveryGuarantee,
    DeliveryRetryPolicy,
    DispatchRequest,
    OutboxDispatcher,
)
from monitoring_kit.runtime.ports import DeliveryLeaseLostError
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.support import ManualClock, ScriptedGateway, TestCollectionAdapter, TestContentPolicy, request


def _peer_bundle():
    return open_persistence(
        PersistenceConfig(
            os.environ["MONITORING_KIT_TEST_MYSQL_URL"],
            delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE,
        )
    )


def _engine(bundle, clock: ManualClock) -> CollectionEngine:
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    return CollectionEngine(
        bundle.run_state_store,
        ContentHistory(bundle.history_store, registry, clock=clock),
        registry,
        ScriptedGateway([]),
        clock=clock,
        worker_id="mysql-test-submitter",
    )


def _observation(clock: ManualClock, record_id: str, body: str = "v1") -> Observation:
    now = clock.now()
    return Observation(
        observation_id=f"mysql-observation-{record_id}",
        scope_key="mysql-outbox-scope",
        run_id=f"mysql-run-{record_id}",
        ingest_key=IngestKey("mysql-test-gateway", record_id),
        subject=SubjectRef("test.content", "mysql-subject", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope(
            "test.content",
            "1.0",
            {"key": "mysql-subject", "body": body},
        ),
        provenance=Provenance(),
        received_at=now,
    )


class _Publisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events = []

    def publish(self, events) -> None:
        if self.fail:
            self.fail = False
            raise RuntimeError("MySQL Outbox 模拟临时传输失败")
        self.events.extend(events)


def test_mysql_allocator_is_exclusive_across_independent_connections(mysql_persistence):
    clock = ManualClock()
    engine = _engine(mysql_persistence, clock)
    for index in range(4):
        engine.submit_run(
            request(f"mysql-run-{index}"),
            ExecutionContext("mysql-allocation-scope", "mysql-test", f"key-{index}"),
        )

    def allocate(worker_id: str):
        return mysql_persistence.run_state_store.allocate(
            AllocationRequest(worker_id, clock.now(), batch_size=2, lease_seconds=30)
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        batches = list(workers.map(allocate, ("mysql-worker-a", "mysql-worker-b")))

    claimed = [record for batch in batches for record in batch]
    assert len(claimed) == 4
    assert len({record.run_id for record in claimed}) == 4


def test_mysql_allocator_does_not_oversell_a_shared_global_limit(mysql_persistence):
    peer = _peer_bundle()
    try:
        clock = ManualClock()
        engine = _engine(mysql_persistence, clock)
        for index in range(2):
            engine.submit_run(
                request(f"mysql-global-{index}"),
                ExecutionContext("mysql-global-scope", "mysql-test", f"global-key-{index}"),
            )

        def allocate(worker_id: str):
            store = mysql_persistence.run_state_store if worker_id.endswith("a") else peer.run_state_store
            return store.allocate(
                AllocationRequest(
                    worker_id=f"mysql-{worker_id}",
                    now=clock.now(),
                    batch_size=1,
                    lease_seconds=30,
                    global_concurrency_limit=1,
                )
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            batches = list(workers.map(allocate, ("worker-a", "worker-b")))

        claimed = [record for batch in batches for record in batch]
        assert len(claimed) == 1
    finally:
        peer.close()


def test_mysql_run_lease_fencing_survives_independent_connections(mysql_persistence):
    peer = _peer_bundle()
    try:
        clock = ManualClock()
        ref = _engine(mysql_persistence, clock).submit_run(
            request("mysql-fence"),
            ExecutionContext("mysql-fence-scope", "mysql-test", "mysql-fence-key"),
        )
        old_copy = mysql_persistence.run_state_store.allocate(
            AllocationRequest("mysql-worker-a", clock.now(), batch_size=1, lease_seconds=10)
        )[0]

        clock.advance(11)
        current_copy = peer.run_state_store.allocate(
            AllocationRequest("mysql-worker-b", clock.now(), batch_size=1, lease_seconds=10)
        )[0]
        assert current_copy.run_id == ref.run_id
        assert current_copy.lease_owner == "mysql-worker-b"

        old_copy.cursor = "stale-cursor"
        with pytest.raises(RunStateConflictError):
            mysql_persistence.run_state_store.save(old_copy)

        restored = peer.run_state_store.get("mysql-fence-scope", ref.run_id)
        assert restored is not None
        assert restored.lease_owner == "mysql-worker-b"
        assert restored.cursor is None
    finally:
        peer.close()


def test_mysql_outbox_claim_is_exclusive_across_independent_connections(mysql_persistence):
    peer = _peer_bundle()
    try:
        clock = ManualClock()
        registry = ExtensionRegistry()
        registry.register_content_policy(TestContentPolicy())
        ContentHistory(mysql_persistence.history_store, registry, clock=clock).record(
            _observation(clock, "claim-once")
        )

        def claim(worker_id: str):
            return mysql_persistence.event_delivery_store.claim_pending(
                DispatchRequest(worker_id, clock.now(), batch_size=1, lease_seconds=10)
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            batches = list(workers.map(claim, ("mysql-dispatcher-a", "mysql-dispatcher-b")))

        claims = [claim for batch in batches for claim in batch]
        assert len(claims) == 1
        assert claims[0].event.event_id
    finally:
        peer.close()


def test_mysql_outbox_lease_recovery_fences_stale_ack(mysql_persistence):
    peer = _peer_bundle()
    try:
        clock = ManualClock()
        registry = ExtensionRegistry()
        registry.register_content_policy(TestContentPolicy())
        ContentHistory(mysql_persistence.history_store, registry, clock=clock).record(
            _observation(clock, "lease-recovery")
        )

        old_claim = mysql_persistence.event_delivery_store.claim_pending(
            DispatchRequest("mysql-dispatcher-a", clock.now(), lease_seconds=1)
        )[0]
        clock.advance(2)
        new_claim = peer.event_delivery_store.claim_pending(
            DispatchRequest("mysql-dispatcher-b", clock.now(), lease_seconds=10)
        )[0]

        with pytest.raises(DeliveryLeaseLostError):
            mysql_persistence.event_delivery_store.mark_delivered(old_claim, clock.now())
        peer.event_delivery_store.mark_delivered(new_claim, clock.now())
        assert peer.event_delivery_store.claim_pending(
            DispatchRequest("mysql-dispatcher-c", clock.now())
        ) == ()
    finally:
        peer.close()


def test_mysql_dispatcher_retries_and_confirms_through_a_peer_connection(mysql_persistence):
    peer = _peer_bundle()
    try:
        clock = ManualClock()
        registry = ExtensionRegistry()
        registry.register_content_policy(TestContentPolicy())
        result = ContentHistory(mysql_persistence.history_store, registry, clock=clock).record(
            _observation(clock, "dispatcher-retry")
        )

        failed = OutboxDispatcher(
            mysql_persistence.event_delivery_store,
            _Publisher(fail=True),
            retry_policy=DeliveryRetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=2),
        ).dispatch_once(DispatchRequest("mysql-dispatcher-a", clock.now()))
        assert (failed.claimed, failed.retried, failed.delivered) == (1, 1, 0)

        clock.advance(2)
        publisher = _Publisher()
        delivered = OutboxDispatcher(
            peer.event_delivery_store,
            publisher,
            retry_policy=DeliveryRetryPolicy(max_attempts=3, base_delay_seconds=2, max_delay_seconds=2),
        ).dispatch_once(DispatchRequest("mysql-dispatcher-b", clock.now()))
        assert (delivered.claimed, delivered.delivered) == (1, 1)
        assert [event.event_id for event in publisher.events] == [result.events[0].event_id]
    finally:
        peer.close()


def test_mysql_history_and_outbox_commit_roll_back_together(mysql_persistence):
    clock = ManualClock()
    registry = ExtensionRegistry()
    registry.register_content_policy(TestContentPolicy())
    history = ContentHistory(mysql_persistence.history_store, registry, clock=clock)
    stored = history.record(_observation(clock, "atomic-one"))
    bad_observation = replace(
        stored.observation,
        observation_id="mysql-bad-observation",
        run_id="mysql-bad-run",
        ingest_key=IngestKey("mysql-test-gateway", "atomic-bad"),
    )
    bad_result = replace(stored, observation=bad_observation)

    with pytest.raises(PersistenceInvariantError):
        mysql_persistence.history_store.commit(
            HistoryWrite(
                ("mysql-test-gateway", "atomic-bad"),
                "mysql-bad-fingerprint",
                bad_result,
                stored.document,
            )
        )

    assert mysql_persistence.history_store.get_by_ingest_key(
        "mysql-outbox-scope",
        IngestKey("mysql-test-gateway", "atomic-bad"),
    ) is None
    claims = mysql_persistence.event_delivery_store.claim_pending(
        DispatchRequest("mysql-atomic-checker", clock.now(), batch_size=10)
    )
    assert [claim.event.event_id for claim in claims] == [stored.events[0].event_id]
