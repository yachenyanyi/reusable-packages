from __future__ import annotations

import threading

import pytest

from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore
from monitoring_kit.contracts import (
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
    TypedEnvelope,
)
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.support import ManualClock, TestContentPolicy


@pytest.fixture(params=("memory", "sqlite", "mysql"), ids=("memory", "sqlite", "mysql"))
def history_backend(request, tmp_path):
    if request.param == "memory":
        yield InMemoryHistoryStore(), lambda: None
        return

    if request.param == "mysql":
        bundle = request.getfixturevalue("mysql_persistence")
        yield bundle.history_store, lambda: None
        return

    pytest.importorskip("sqlalchemy")
    from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence

    bundle = open_persistence(PersistenceConfig(f"sqlite:///{(tmp_path / 'history-concurrency.db').as_posix()}"))
    try:
        yield bundle.history_store, bundle.close
    finally:
        bundle.close()


def _registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register_content_policy(TestContentPolicy())
    return registry


def _observation(record_id: str, body: str) -> Observation:
    now = ManualClock().now()
    return Observation(
        observation_id=f"observation-{record_id}",
        scope_key="scope-a",
        run_id=f"run-{record_id}",
        ingest_key=IngestKey("concurrent-gateway", record_id),
        subject=SubjectRef("test.content", "same-subject", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("test.content", "1.0", {"key": "same-subject", "body": body}),
        provenance=Provenance(),
        received_at=now,
    )


class _FirstReadBarrierStore:
    """让两个 HistoryWrite 都先基于不存在的 Document 完成计算。"""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._document_reads = 0

    def get_document(self, scope_key, subject):
        value = self._delegate.get_document(scope_key, subject)
        with self._lock:
            self._document_reads += 1
            first_two = self._document_reads <= 2
        if first_two:
            assert self._barrier.wait(5) in (0, 1)
        return value

    def __getattr__(self, name):
        return getattr(self._delegate, name)


def test_concurrent_first_observations_keep_document_history_consistent(history_backend):
    store, close = history_backend
    try:
        clock = ManualClock()
        delegate = store
        concurrent_store = _FirstReadBarrierStore(delegate)
        histories = [
            ContentHistory(concurrent_store, _registry(), clock=clock)
            for _ in range(2)
        ]
        observations = [_observation("one", "v1"), _observation("two", "v2")]
        results = []
        errors = []

        def record(history, observation):
            try:
                results.append(history.record(observation))
            except Exception as exc:  # 由主线程统一报告并保留 traceback
                errors.append(exc)

        threads = [
            threading.Thread(target=record, args=(history, observation))
            for history, observation in zip(histories, observations)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        assert not any(thread.is_alive() for thread in threads)
        assert not errors
        assert len(results) == 2

        timeline = delegate.get_timeline("scope-a", results[0].document.document_id)
        assert timeline is not None
        assert [snapshot.revision for snapshot in timeline.snapshots] == [1, 2]
        assert [event.sequence for event in timeline.events] == [1, 2]
        assert timeline.document.current_snapshot_id == timeline.snapshots[-1].snapshot_id
        assert {snapshot.content.data["body"] for snapshot in timeline.snapshots} == {"v1", "v2"}
    finally:
        close()
