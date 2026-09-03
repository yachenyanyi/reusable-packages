from __future__ import annotations

import pytest

from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore, InMemoryRunStateStore
from monitoring_kit.contracts import ExecutionContext, RuntimePolicy
from monitoring_kit.collection.engine import CollectionEngine
from monitoring_kit.collection.ports import RunStateConflictError
from monitoring_kit.errors import ConfigurationError
from monitoring_kit.runtime import AllocationRequest
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.support import ManualClock, ScriptedGateway, TestCollectionAdapter, TestContentPolicy, request
from monitoring_kit.history.history import ContentHistory


@pytest.fixture(params=("memory", "sqlite", "mysql"), ids=("memory", "sqlite", "mysql"))
def allocation_backend(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRunStateStore(), lambda: None
        return

    if request.param == "mysql":
        bundle = request.getfixturevalue("mysql_persistence")
        yield bundle.run_state_store, lambda: None
        return

    pytest.importorskip("sqlalchemy")
    from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence

    bundle = open_persistence(PersistenceConfig(f"sqlite:///{(tmp_path / 'allocation.db').as_posix()}"))
    try:
        yield bundle.run_state_store, bundle.close
    finally:
        bundle.close()


def _engine(store, clock: ManualClock) -> CollectionEngine:
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    history = ContentHistory(InMemoryHistoryStore(), registry, clock=clock)
    return CollectionEngine(
        store,
        history,
        registry,
        ScriptedGateway([]),
        clock=clock,
        worker_id="submitter",
    )


def _submit(engine, scope: str, key: str, policy: RuntimePolicy, gateway_hint: str | None = None):
    context = ExecutionContext(scope, "tester", key, runtime_policy=policy)
    return engine.submit_run(request(key, gateway_hint=gateway_hint), context)


def _allocation(store, clock: ManualClock, worker: str, *, batch_size: int = 10, global_limit=None):
    return store.allocate(
        AllocationRequest(
            worker_id=worker,
            now=clock.now(),
            batch_size=batch_size,
            lease_seconds=10,
            global_concurrency_limit=global_limit,
        )
    )


def _release(store, record):
    record.lease_owner = None
    record.lease_until = None
    store.save(record)


def test_round_robin_fairness_keeps_a_busy_scope_from_monopolizing_allocation(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    policy = RuntimePolicy()
    for index in range(3):
        _submit(engine, "scope-a", f"a-{index}", policy)
        _submit(engine, "scope-b", f"b-{index}", policy)

    scopes = []
    for index in range(4):
        claim = _allocation(store, clock, f"worker-{index}", batch_size=1)[0]
        scopes.append(claim.scope_key)
        _release(store, claim)

    assert scopes == ["scope-a", "scope-b", "scope-a", "scope-b"]


def test_scope_limit_counts_leases_already_owned_by_another_worker(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    policy = RuntimePolicy(max_concurrent_runs=1)
    _submit(engine, "scope-a", "a-1", policy)
    _submit(engine, "scope-a", "a-2", policy)

    first = _allocation(store, clock, "worker-a", batch_size=1)
    assert len(first) == 1
    assert _allocation(store, clock, "worker-b", batch_size=10) == ()

    clock.advance(11)
    assert len(_allocation(store, clock, "worker-b", batch_size=10)) == 1


def test_global_limit_is_shared_by_workers_and_releases_after_lease_expiry(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    policy = RuntimePolicy()
    _submit(engine, "scope-a", "a", policy)
    _submit(engine, "scope-b", "b", policy)

    assert len(_allocation(store, clock, "worker-a", global_limit=1)) == 1
    assert _allocation(store, clock, "worker-b", global_limit=1) == ()
    clock.advance(11)
    assert len(_allocation(store, clock, "worker-b", global_limit=1)) == 1


def test_gateway_limit_skips_saturated_gateway_but_keeps_other_gateway_running(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    policy = RuntimePolicy(gateway_limits={"gateway-a": 1})
    _submit(engine, "scope-a", "a", policy, "gateway-a")
    clock.advance(1)
    _submit(engine, "scope-a", "b", policy, "gateway-b")

    first = _allocation(store, clock, "worker-a", batch_size=1)
    assert first[0].upstream_request.gateway_hint == "gateway-a"
    second = _allocation(store, clock, "worker-b", batch_size=10)
    assert len(second) == 1
    assert second[0].upstream_request.gateway_hint == "gateway-b"


def test_gateway_limit_policy_requires_a_resolved_gateway_hint():
    store = InMemoryRunStateStore()
    clock = ManualClock()
    engine = _engine(store, clock)

    with pytest.raises(ConfigurationError, match="gateway_hint"):
        _submit(engine, "scope-a", "without-hint", RuntimePolicy(gateway_limits={"gateway-a": 1}))


def test_same_worker_can_renew_when_global_limit_is_already_full(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    _submit(engine, "scope-a", "a", RuntimePolicy())

    first = _allocation(store, clock, "worker-a", batch_size=1, global_limit=1)
    renewed = _allocation(store, clock, "worker-a", batch_size=1, global_limit=1)

    assert len(first) == 1
    assert len(renewed) == 1
    assert renewed[0].run_id == first[0].run_id


def test_saturated_gateway_does_not_hide_a_later_available_candidate(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    policy = RuntimePolicy(gateway_limits={"gateway-a": 1})
    for index in range(3):
        _submit(engine, "scope-a", f"a-{index}", policy, "gateway-a")
        clock.advance(1)
    _submit(engine, "scope-a", "gateway-b", policy, "gateway-b")

    first = _allocation(store, clock, "worker-a", batch_size=1)
    second = _allocation(store, clock, "worker-b", batch_size=1)

    assert first[0].upstream_request.gateway_hint == "gateway-a"
    assert second[0].upstream_request.gateway_hint == "gateway-b"


def test_expired_run_lease_is_reclaimable_and_old_copy_is_fenced(allocation_backend):
    store, _close = allocation_backend
    clock = ManualClock()
    engine = _engine(store, clock)
    _submit(engine, "scope-a", "a", RuntimePolicy())

    old_copy = _allocation(store, clock, "worker-a", batch_size=1)[0]
    clock.advance(11)
    current_copy = _allocation(store, clock, "worker-b", batch_size=1)[0]
    assert current_copy.run_id == old_copy.run_id
    old_copy.cursor = "stale"
    with pytest.raises(RunStateConflictError):
        store.save(old_copy)
