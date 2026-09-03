from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from monitoring_kit.adapters.events.memory import InMemoryEventSink
from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore
from monitoring_kit.contracts import (
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
    TypedEnvelope,
)
from monitoring_kit.errors import ConfigurationError
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.history.model import HistoryWrite
from monitoring_kit.runtime import (
    DeliveryGuarantee,
    DeliveryRetryPolicy,
    DispatchRequest,
    OutboxDispatcher,
)
from monitoring_kit.runtime.ports import DeliveryLeaseLostError
from monitoring_kit.runtime.registry import ExtensionRegistry
from monitoring_kit.adapters.persistence.relational import PersistenceInvariantError

from tests.support import ManualClock, TestCollectionAdapter, TestContentPolicy


def _registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    return registry


def _observation(
    clock: ManualClock,
    *,
    record_id: str,
    body: str,
    scope_key: str = "scope-a",
    key: str = "key",
) -> Observation:
    now = clock.now()
    return Observation(
        observation_id=f"observation-{record_id}",
        scope_key=scope_key,
        run_id=f"run-{record_id}",
        ingest_key=IngestKey("test-gateway", record_id),
        subject=SubjectRef("test.content", key, "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("test.content", "1.0", {"key": key, "body": body}),
        provenance=Provenance(),
        received_at=now,
    )


class RecordingPublisher:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.events = []
        self.calls = 0

    def publish(self, events) -> None:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("publisher temporarily unavailable")
        self.events.extend(events)


class _ProcessCrash(BaseException):
    pass


class _CrashPublisher:
    def __init__(self, *, after_publish: bool) -> None:
        self.after_publish = after_publish
        self.events = []

    def publish(self, events) -> None:
        if self.after_publish:
            self.events.extend(events)
        raise _ProcessCrash("模拟 Dispatcher 进程中断")


def test_outbox_retries_a_temporary_publish_failure_and_keeps_event_id():
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    history = ContentHistory(store, _registry(), clock=clock)
    result = history.record(_observation(clock, record_id="one", body="v1"))
    publisher = RecordingPublisher(failures=1)
    dispatcher = OutboxDispatcher(
        store,
        publisher,
        retry_policy=DeliveryRetryPolicy(
            max_attempts=3,
            base_delay_seconds=5,
            max_delay_seconds=5,
        ),
    )

    first = dispatcher.dispatch_once(DispatchRequest("worker-a", clock.now(), lease_seconds=10))
    assert (first.claimed, first.retried, first.delivered, first.blocked) == (1, 1, 0, 0)
    assert publisher.events == []

    clock.advance(5)
    second = dispatcher.dispatch_once(DispatchRequest("worker-b", clock.now(), lease_seconds=10))
    assert (second.claimed, second.delivered, second.retried, second.blocked) == (1, 1, 0, 0)
    assert [event.event_id for event in publisher.events] == [result.events[0].event_id]
    assert dispatcher.dispatch_once(DispatchRequest("worker-c", clock.now())).claimed == 0


def test_outbox_blocks_after_retry_budget_without_deleting_the_event():
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    history = ContentHistory(store, _registry(), clock=clock)
    result = history.record(_observation(clock, record_id="one", body="v1"))
    dispatcher = OutboxDispatcher(
        store,
        RecordingPublisher(failures=10),
        retry_policy=DeliveryRetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
    )

    first = dispatcher.dispatch_once(DispatchRequest("worker-a", clock.now()))
    second = dispatcher.dispatch_once(DispatchRequest("worker-a", clock.now()))
    assert (first.retried, first.blocked) == (1, 0)
    assert (second.retried, second.blocked) == (0, 1)
    assert dispatcher.dispatch_once(DispatchRequest("worker-a", clock.now())).claimed == 0

    # BLOCKED 是可观察的保留状态；它不是“投递成功”，事件事实仍然存在。
    claim = store.claim_pending(DispatchRequest("repair-worker", clock.now() + timedelta(days=1)))
    assert claim == ()
    assert result.events[0].event_id


def test_expired_outbox_lease_can_be_reclaimed_but_stale_worker_cannot_ack():
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    ContentHistory(store, _registry(), clock=clock).record(
        _observation(clock, record_id="one", body="v1")
    )
    old_claim = store.claim_pending(DispatchRequest("worker-a", clock.now(), lease_seconds=1))[0]
    clock.advance(2)
    new_claim = store.claim_pending(DispatchRequest("worker-b", clock.now(), lease_seconds=10))[0]

    with pytest.raises(DeliveryLeaseLostError):
        store.mark_delivered(old_claim, clock.now())
    store.mark_delivered(new_claim, clock.now())


@pytest.mark.parametrize(
    "after_publish",
    (False, True),
    ids=("publish前", "publish后确认前"),
)
def test_dispatcher_crash_windows_recover_with_the_same_event_id(after_publish):
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    result = ContentHistory(store, _registry(), clock=clock).record(
        _observation(clock, record_id="crash-window", body="v1")
    )
    first_publisher = _CrashPublisher(after_publish=after_publish)
    dispatcher = OutboxDispatcher(store, first_publisher)

    with pytest.raises(_ProcessCrash):
        dispatcher.dispatch_once(DispatchRequest("crashed-worker", clock.now(), lease_seconds=1))

    clock.advance(2)
    recovered_publisher = RecordingPublisher()
    recovered = OutboxDispatcher(store, recovered_publisher).dispatch_once(
        DispatchRequest("recovered-worker", clock.now(), lease_seconds=10)
    )

    assert (recovered.claimed, recovered.delivered) == (1, 1)
    assert [event.event_id for event in recovered_publisher.events] == [result.events[0].event_id]
    if after_publish:
        assert [event.event_id for event in first_publisher.events] == [result.events[0].event_id]
    else:
        assert first_publisher.events == []


def test_same_document_events_are_claimed_in_sequence_order():
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    history = ContentHistory(store, _registry(), clock=clock)
    first = history.record(_observation(clock, record_id="one", body="v1"))
    clock.advance(1)
    second = history.record(_observation(clock, record_id="two", body="v2"))

    first_claim = store.claim_pending(DispatchRequest("worker-a", clock.now(), batch_size=10))[0]
    assert first_claim.event.event_id == first.events[0].event_id
    assert len(store.claim_pending(DispatchRequest("worker-b", clock.now(), batch_size=10))) == 0
    store.mark_delivered(first_claim, clock.now())
    second_claim = store.claim_pending(DispatchRequest("worker-b", clock.now(), batch_size=10))[0]
    assert second_claim.event.event_id == second.events[0].event_id


def test_duplicate_ingest_does_not_create_a_second_outbox_record():
    clock = ManualClock()
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    history = ContentHistory(store, _registry(), clock=clock)
    observation = _observation(clock, record_id="one", body="v1")
    first = history.record(observation)
    duplicate = history.record(observation)

    assert duplicate.duplicate is True
    claims = store.claim_pending(DispatchRequest("worker-a", clock.now(), batch_size=10))
    assert [claim.event.event_id for claim in claims] == [first.events[0].event_id]


def test_durable_outbox_and_submit_after_commit_event_sink_cannot_be_mixed():
    store = InMemoryHistoryStore(delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE)
    with pytest.raises(ConfigurationError):
        ContentHistory(store, _registry(), event_sink=InMemoryEventSink())


def test_concurrent_same_ingest_only_the_winner_publishes_to_direct_sink():
    clock = ManualClock()
    inner = InMemoryHistoryStore()
    barrier = threading.Barrier(2)

    class BarrierStore:
        delivery_guarantee = DeliveryGuarantee.NONE

        def __init__(self, delegate):
            self.delegate = delegate

        def get_document(self, scope_key, subject):
            value = self.delegate.get_document(scope_key, subject)
            barrier.wait(5)
            return value

        def __getattr__(self, name):
            return getattr(self.delegate, name)

    store = BarrierStore(inner)
    sinks = [InMemoryEventSink(), InMemoryEventSink()]
    histories = [
        ContentHistory(store, _registry(), event_sink=sinks[index], clock=clock)
        for index in range(2)
    ]
    observation = _observation(clock, record_id="one", body="v1")
    results = []
    errors = []

    def record(history):
        try:
            results.append(history.record(observation))
        except Exception as exc:  # pragma: no cover - 失败由下方统一抛出
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(history,)) for history in histories]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert len(results) == 2
    assert sorted(result.duplicate for result in results) == [False, True]
    assert sum(len(sink.events) for sink in sinks) == 1


def test_relational_history_failure_rolls_back_outbox_with_the_history_transaction(tmp_path):
    sa = pytest.importorskip("sqlalchemy")
    from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence

    bundle = open_persistence(
        PersistenceConfig(
            f"sqlite:///{(tmp_path / 'outbox-atomic.db').as_posix()}",
            delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE,
        )
    )
    try:
        history = ContentHistory(bundle.history_store, _registry(), clock=ManualClock())
        stored_result = history.record(_observation(ManualClock(), record_id="one", body="v1"))
        claim = bundle.history_store.claim_pending(DispatchRequest("worker-a", stored_result.observation.received_at))
        bundle.history_store.mark_delivered(claim[0], stored_result.observation.received_at)

        bad_observation = replace(
            stored_result.observation,
            observation_id="bad-observation",
            run_id="bad-run",
            ingest_key=IngestKey("test-gateway", "bad-record"),
        )
        bad_result = replace(stored_result, observation=bad_observation)
        with pytest.raises(PersistenceInvariantError):
            bundle.history_store.commit(
                HistoryWrite(
                    ("test-gateway", "bad-record"),
                    "bad-fingerprint",
                    bad_result,
                    stored_result.document,
                )
            )
        assert bundle.history_store.claim_pending(
            DispatchRequest("worker-b", stored_result.observation.received_at)
        ) == ()
        with bundle._engine.connect() as connection:
            count = connection.execute(sa.text("SELECT COUNT(*) FROM monitoring_kit_history_ingest")).scalar_one()
        assert count == 1
    finally:
        bundle.close()
