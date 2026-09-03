from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest

from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore, InMemoryRunStateStore
from monitoring_kit.collection.engine import CollectionEngine
from monitoring_kit.contracts import (
    ChangeQuery,
    ExecutionContext,
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
    TypedEnvelope,
)
from monitoring_kit.errors import RunNotFoundError
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.history.model import HistoryWrite
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.support import ManualClock, ScriptedGateway, TestCollectionAdapter, TestContentPolicy, request


@dataclass
class StorageSet:
    run_store: object
    history_store: object
    scope: callable
    close: callable


@pytest.fixture(params=("memory", "sqlite", "mysql"), ids=("memory", "sqlite", "mysql"))
def storage_set(request, tmp_path):
    backend = request.param
    prefix = f"scope-contract-{uuid.uuid4().hex}"
    if backend == "memory":
        yield StorageSet(
            InMemoryRunStateStore(),
            InMemoryHistoryStore(),
            lambda name: f"{prefix}-{name}",
            lambda: None,
        )
        return

    pytest.importorskip("sqlalchemy")
    from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence

    if backend == "mysql":
        database_url = os.environ.get("MONITORING_KIT_TEST_MYSQL_URL")
        if not database_url:
            pytest.skip("设置 MONITORING_KIT_TEST_MYSQL_URL 后运行真实 MySQL 契约")
    else:
        database_url = f"sqlite:///{(tmp_path / 'scope-contract.db').as_posix()}"
    bundle = open_persistence(PersistenceConfig(database_url))
    try:
        yield StorageSet(
            bundle.run_state_store,
            bundle.history_store,
            lambda name: f"{prefix}-{name}",
            bundle.close,
        )
    finally:
        bundle.close()


def _registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    return registry


def _engine(storage: StorageSet, clock: ManualClock) -> CollectionEngine:
    registry = _registry()
    return CollectionEngine(
        storage.run_store,
        ContentHistory(storage.history_store, registry, clock=clock),
        registry,
        ScriptedGateway([]),
        clock=clock,
        worker_id="scope-contract-worker",
    )


def _observation(scope: str, record_id: str, body: str = "v1") -> Observation:
    now = ManualClock().now()
    return Observation(
        observation_id=f"observation-{record_id}",
        scope_key=scope,
        run_id=f"run-{record_id}",
        ingest_key=IngestKey("scope-gateway", record_id),
        subject=SubjectRef("test.content", "same-subject", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("test.content", "1.0", {"key": "same-subject", "body": body}),
        provenance=Provenance(),
        received_at=now,
    )


def test_same_idempotency_key_is_independent_between_scopes(storage_set):
    clock = ManualClock()
    engine = _engine(storage_set, clock)
    scope_a = storage_set.scope("a")
    scope_b = storage_set.scope("b")
    first = engine.submit_run(request("same-request"), ExecutionContext(scope_a, "actor", "same-key"))
    second = engine.submit_run(request("same-request"), ExecutionContext(scope_b, "actor", "same-key"))

    assert first.run_id != second.run_id
    assert storage_set.run_store.get(scope_a, first.run_id) is not None
    assert storage_set.run_store.get(scope_b, second.run_id) is not None
    assert storage_set.run_store.get(scope_b, first.run_id) is None


def test_wrong_scope_cannot_read_cancel_or_mutate_a_run(storage_set):
    clock = ManualClock()
    engine = _engine(storage_set, clock)
    scope_a = storage_set.scope("a")
    scope_b = storage_set.scope("b")
    ref = engine.submit_run(request("run"), ExecutionContext(scope_a, "actor", "run-key"))

    with pytest.raises(RunNotFoundError):
        engine.get_run(ref.run_id, scope_b)
    with pytest.raises(RunNotFoundError):
        engine.cancel_run(ref.run_id, ExecutionContext(scope_b, "actor", "cancel-key"))

    record = storage_set.run_store.get(scope_a, ref.run_id)
    record.scope_key = scope_b
    with pytest.raises(ValueError):
        storage_set.run_store.save(record)
    restored = storage_set.run_store.get(scope_a, ref.run_id)
    assert restored is not None
    assert restored.scope_key == scope_a


def test_same_subject_and_ingest_key_are_independent_between_scopes(storage_set):
    clock = ManualClock()
    history = ContentHistory(storage_set.history_store, _registry(), clock=clock)
    scope_a = storage_set.scope("a")
    scope_b = storage_set.scope("b")
    first = history.record(_observation(scope_a, "same-record"))
    second = history.record(_observation(scope_b, "same-record"))
    duplicate = history.record(_observation(scope_a, "same-record"))

    assert first.document.document_id != second.document.document_id
    assert duplicate.duplicate is True
    assert storage_set.history_store.get_document(scope_a, first.document.subject) is not None
    assert storage_set.history_store.get_document(scope_b, second.document.subject) is not None
    assert storage_set.history_store.get_document(scope_b, first.document.subject).document_id == second.document.document_id


def test_wrong_scope_cannot_read_history_or_reuse_a_change_cursor(storage_set):
    clock = ManualClock()
    history = ContentHistory(storage_set.history_store, _registry(), clock=clock)
    scope_a = storage_set.scope("a")
    scope_b = storage_set.scope("b")
    result = history.record(_observation(scope_a, "history-record"))

    assert history.get_current(result.document.document_id, scope_b) is None
    assert history.get_timeline(result.document.document_id, scope_b) is None
    assert history.query_changes(ChangeQuery(limit=10), scope_b).events == ()
    page = history.query_changes(ChangeQuery(limit=10), scope_a)
    assert len(page.events) == 1
    assert history.query_changes(
        ChangeQuery(cursor=page.next_cursor, limit=10), scope_b
    ).events == ()


def test_history_write_rejects_mixed_scope_before_store_is_called(storage_set):
    clock = ManualClock()
    history = ContentHistory(storage_set.history_store, _registry(), clock=clock)
    scope_a = storage_set.scope("a")
    result = history.record(_observation(scope_a, "valid-record"))
    mixed_document = result.document.__class__(
        document_id=result.document.document_id,
        scope_key=storage_set.scope("b"),
        subject=result.document.subject,
        state=result.document.state,
        current_snapshot_id=result.document.current_snapshot_id,
        first_observed_at=result.document.first_observed_at,
        last_observed_at=result.document.last_observed_at,
        missing_streak=result.document.missing_streak,
        policy_ref=result.document.policy_ref,
    )

    with pytest.raises(ValueError):
        HistoryWrite(
            ("scope-gateway", "new-record"),
            "fingerprint",
            result.__class__(
                observation=_observation(scope_a, "new-record"),
                document=mixed_document,
                snapshot=result.snapshot,
                events=result.events,
            ),
            None,
        )
