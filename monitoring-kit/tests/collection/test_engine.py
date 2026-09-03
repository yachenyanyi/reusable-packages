import pytest

from monitoring_kit.collection.engine import RetryPolicy
from monitoring_kit.collection.ports import UpstreamError
from monitoring_kit.contracts import ChangeQuery, ExecutionContext, ExecutionLimits, RunStatus, TypedEnvelope
from monitoring_kit.errors import (
    IdempotencyConflictError,
    InvalidCollectionSpecError,
    ScopeMismatchError,
    UnsupportedCollectionTypeError,
)

from tests.support import ScriptedGateway, build_engine, context, request


def test_submit_is_idempotent_and_conflicts_on_changed_request():
    engine, _, _, _, gateway, _ = build_engine([])
    first = engine.submit_run(request("a"), context("same"))
    second = engine.submit_run(request("a"), context("same"))
    assert first.run_id == second.run_id
    assert gateway.submit_calls == 0
    with pytest.raises(IdempotencyConflictError):
        engine.submit_run(request("b"), context("same"))


def test_submit_validates_registered_collection_type_and_spec():
    engine, _, _, _, _, _ = build_engine([])
    with pytest.raises(InvalidCollectionSpecError):
        engine.submit_run(
            request("x").__class__(
                collection=TypedEnvelope(
                    "test.collection", "1.0", {"invalid": True, "records": []}
                ),
                source_ref="test-source",
            ),
            context("invalid"),
        )
    with pytest.raises(UnsupportedCollectionTypeError):
        engine.submit_run(
            request("x").__class__(
                collection=TypedEnvelope(
                    "unknown.collection", "1.0", {}
                )
            ),
            context("unknown"),
        )


def test_wake_completes_run_and_exposes_only_stable_change_query():
    engine, _, history_store, sink, gateway, _ = build_engine(
        [
            {"record_id": "r1", "payload": {"key": "one", "body": "v1"}},
            {"record_id": "r2", "payload": {"key": "two", "body": "v1"}},
        ],
    )
    ref = engine.submit_run(request(), context())
    work = engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert work.completed == 1
    assert summary.status is RunStatus.COMPLETED
    assert summary.processed_count == 2
    assert summary.change_count == 2
    assert not hasattr(summary, "upstream_job_id")
    assert len(sink.events) == 2
    assert gateway.submit_calls == 1
    page = engine.query_changes(ChangeQuery(limit=1), "scope-a")
    assert len(page.events) == 1
    assert page.has_more is True
    page2 = engine.query_changes(ChangeQuery(cursor=page.next_cursor, limit=10), "scope-a")
    assert len(page2.events) == 1
    timeline = history_store.get_timeline("scope-a", page.events[0].document_id)
    assert timeline is not None
    assert timeline.snapshots[0].provenance.attempt_ref is not None


def test_scope_isolation_applies_to_run_queries_and_cancellation():
    engine, _, _, _, _, _ = build_engine([])
    ref = engine.submit_run(request(), context("isolated", "scope-a"))
    with pytest.raises(ScopeMismatchError):
        engine.get_run(ref.run_id, "scope-b")
    with pytest.raises(ScopeMismatchError):
        engine.cancel_run(ref.run_id, context("cancel", "scope-b"))
    assert engine.query_changes(ChangeQuery(), "scope-b").events == ()


def test_retry_uses_backoff_and_eventually_succeeds():
    gateway = ScriptedGateway(
        [],
        submit_failures=[
            UpstreamError("TEMPORARY", "暂时不可用", retryable=True),
            UpstreamError("TEMPORARY", "暂时不可用", retryable=True),
        ],
    )
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=10, poll_interval_seconds=0, lease_seconds=30)
    engine, _, _, _, gateway, clock = build_engine([], gateway=gateway, retry_policy=policy)
    ref = engine.submit_run(request(), context("retry"))
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.RUNNING
    assert gateway.submit_calls == 1
    engine.wake()
    assert gateway.submit_calls == 1
    clock.advance(2)
    engine.wake()
    assert gateway.submit_calls == 2
    clock.advance(4)
    engine.wake()
    assert gateway.submit_calls == 3
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED


def test_non_retryable_upstream_failure_is_hidden_as_run_failure():
    gateway = ScriptedGateway([], submit_failures=[UpstreamError("UNAUTHORIZED", "没有权限")])
    engine, _, _, _, _, _ = build_engine([], gateway=gateway)
    ref = engine.submit_run(request(), context("failed"))
    engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert summary.status is RunStatus.FAILED
    assert summary.error is not None
    assert summary.error.code == "UNAUTHORIZED"


def test_cancel_queued_run_is_idempotent_and_does_not_submit_upstream_job():
    engine, _, _, _, gateway, _ = build_engine([])
    ref = engine.submit_run(request(), context("cancel-queued"))
    result = engine.cancel_run(ref.run_id, context("cancel-request", "scope-a"))
    assert result.accepted is True
    assert result.status is RunStatus.CANCELLED
    assert gateway.submit_calls == 0
    assert engine.cancel_run(ref.run_id, context("cancel-again", "scope-a")).status is RunStatus.CANCELLED


def test_result_limit_finishes_with_error_and_requests_upstream_cancellation():
    records = [
        {"record_id": "one", "payload": {"key": "one", "body": "v1"}},
        {"record_id": "two", "payload": {"key": "two", "body": "v1"}},
    ]
    engine, _, _, _, gateway, _ = build_engine(records)
    ref = engine.submit_run(
        request(),
        ExecutionContext("scope-a", "actor", "limited", ExecutionLimits(max_records=1)),
    )
    engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert summary.status is RunStatus.COMPLETED_WITH_ERRORS
    assert summary.error is not None
    assert summary.error.code == "RESULT_LIMIT_REACHED"
    assert summary.processed_count == 1
    assert gateway.cancel_calls == 1


def test_completed_upstream_is_not_marked_done_until_all_pages_are_drained():
    records = [
        {"record_id": "one", "payload": {"key": "one", "body": "v1"}},
        {"record_id": "two", "payload": {"key": "two", "body": "v1"}},
        {"record_id": "three", "payload": {"key": "three", "body": "v1"}},
    ]
    gateway = ScriptedGateway(records, page_size=1)
    policy = RetryPolicy(poll_interval_seconds=0, max_batches_per_wake=1, lease_seconds=30)
    engine, _, _, _, _, _ = build_engine(records, gateway=gateway, retry_policy=policy)
    ref = engine.submit_run(request(), context("pages"))
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.RUNNING
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.RUNNING
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
    assert engine.get_run(ref.run_id, "scope-a").processed_count == 3
