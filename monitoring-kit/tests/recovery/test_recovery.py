from monitoring_kit.contracts import RunStatus

from tests.support import build_engine, context, request


def test_recovery_requeues_interrupted_run_and_does_not_duplicate_history():
    engine, state_store, history_store, sink, gateway, clock = build_engine(
        [{"record_id": "same", "payload": {"key": "key", "body": "body"}}],
    )
    ref = engine.submit_run(request(), context("recover"))
    record = state_store.get("scope-a", ref.run_id)
    record.status = RunStatus.RUNNING
    record.started_at = clock.now()
    record.next_wakeup_at = clock.now()
    state_store.save(record)
    recovered = engine.recover_interrupted_runs()
    assert recovered.recovered == 1
    assert state_store.get("scope-a", ref.run_id).status is RunStatus.QUEUED
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
    assert len(sink.events) == 1


def test_duplicate_result_in_later_page_is_ignored_by_ingest_key():
    from datetime import UTC, datetime

    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    gateway_records = [
        {"record_id": "one", "observed_at": observed_at, "payload": {"key": "one", "body": "v1"}},
        {"record_id": "one", "observed_at": observed_at, "payload": {"key": "one", "body": "v1"}},
        {"record_id": "two", "observed_at": observed_at, "payload": {"key": "two", "body": "v1"}},
    ]
    from tests.support import ScriptedGateway

    gateway = ScriptedGateway(gateway_records, page_size=1)
    engine, _, _, sink, _, _ = build_engine(gateway_records, gateway=gateway)
    ref = engine.submit_run(request(), context("duplicate"))
    engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert summary.status is RunStatus.COMPLETED
    assert summary.processed_count == 2
    assert len(sink.events) == 2


def test_same_record_id_with_changed_content_is_not_silently_ignored():
    records = [
        {"record_id": "same", "payload": {"key": "one", "body": "v1"}},
        {"record_id": "same", "payload": {"key": "one", "body": "v2"}},
    ]
    from tests.support import ScriptedGateway

    gateway = ScriptedGateway(records, page_size=1)
    engine, _, _, sink, _, _ = build_engine(records, gateway=gateway)
    ref = engine.submit_run(request(), context("changed-duplicate"))
    engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert summary.status is RunStatus.COMPLETED_WITH_ERRORS
    assert summary.failed_count == 1
    assert len(sink.events) == 1


def test_transient_history_store_failure_does_not_advance_record_or_cursor():
    engine, _, history_store, _, _, clock = build_engine(
        [{"record_id": "one", "payload": {"key": "one", "body": "v1"}}],
    )

    class FlakyHistoryStore:
        def __init__(self, inner):
            self.inner = inner
            self.failed = False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def commit(self, write):
            if not self.failed:
                self.failed = True
                raise RuntimeError("存储暂时不可用")
            return self.inner.commit(write)

    engine._history._store = FlakyHistoryStore(history_store)
    ref = engine.submit_run(request(), context("flaky-history"))
    engine.wake()
    first = engine.get_run(ref.run_id, "scope-a")
    assert first.status is RunStatus.RUNNING
    assert first.failed_count == 0
    clock.advance(1)
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
