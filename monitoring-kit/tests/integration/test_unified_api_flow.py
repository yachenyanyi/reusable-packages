import pytest

from monitoring_kit.adapters.upstream.unified_api import UnifiedApiConfig, UnifiedApiGateway
from monitoring_kit.adapters.events.memory import InMemoryEventSink
from monitoring_kit.adapters.persistence.memory import InMemoryHistoryStore, InMemoryRunStateStore
from monitoring_kit.collection.engine import CollectionEngine, RetryPolicy
from monitoring_kit.collection.model import UpstreamJobRequest
from monitoring_kit.collection.ports import UpstreamError
from monitoring_kit.contracts import RunStatus
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.fixtures.unified_api import ApiScenario, FakeUnifiedApi, ResponsePlan
from tests.support import ManualClock, TestCollectionAdapter, TestContentPolicy, context, request


def _build_http_engine(api: FakeUnifiedApi, *, clock: ManualClock, max_batches_per_wake: int = 100):
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    history_store = InMemoryHistoryStore()
    event_sink = InMemoryEventSink()
    history = ContentHistory(history_store, registry, event_sink=event_sink, clock=clock)
    gateway = UnifiedApiGateway(UnifiedApiConfig(api.base_url, api.scenario.auth_token), gateway_key="fake-http")
    engine = CollectionEngine(
        InMemoryRunStateStore(),
        history,
        registry,
        gateway,
        clock=clock,
        retry_policy=RetryPolicy(
            base_delay_seconds=2,
            max_delay_seconds=10,
            poll_interval_seconds=0,
            lease_seconds=30,
            max_batches_per_wake=max_batches_per_wake,
        ),
        worker_id="http-test-worker",
    )
    return engine, history_store, event_sink, gateway


def test_real_http_gateway_and_engine_complete_a_two_page_job():
    scenario = ApiScenario(
        records=(
            {"record_id": "r1", "payload": {"key": "one", "body": "v1"}},
            {"record_id": "r2", "payload": {"key": "two", "body": "v1"}},
        ),
        page_size=1,
        status_timeline=("queued", "completed"),
        auth_token="test-secret",
    )
    clock = ManualClock()
    with FakeUnifiedApi(scenario) as api:
        engine, history_store, event_sink, _ = _build_http_engine(api, clock=clock, max_batches_per_wake=1)
        ref = engine.submit_run(request(), context("http-happy"))

        first = engine.wake()
        assert first.deferred == 1
        assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.RUNNING
        second = engine.wake()
        assert second.completed == 1
        summary = engine.get_run(ref.run_id, "scope-a")
        assert summary.status is RunStatus.COMPLETED
        assert summary.processed_count == 2
        assert len(event_sink.events) == 2
        timeline = history_store.get_timeline("scope-a", event_sink.events[0].document_id)
        assert timeline is not None
        assert len(timeline.events) == 1

        result_requests = [item for item in api.requests if item["path"].endswith("/results")]
        assert len(result_requests) == 2
        assert result_requests[0]["query"] == {}
        assert result_requests[1]["query"] == {"cursor": ["1"]}
        submit = next(item for item in api.requests if item["method"] == "POST" and item["path"] == "/jobs")
        assert submit["body"]["idempotency_key"] == ref.run_id
        assert submit["headers"]["authorization"] == "Bearer test-secret"


def test_submission_side_effect_and_error_are_reconciled_by_idempotency():
    scenario = ApiScenario(
        records=({"record_id": "r1", "payload": {"key": "one", "body": "v1"}},),
        submission_script=(
            ResponsePlan(
                status_code=503,
                body={"code": "TEMPORARY", "message": "try later", "retryable": True},
            ),
        ),
        auth_token="test-secret",
    )
    clock = ManualClock()
    with FakeUnifiedApi(scenario) as api:
        engine, _, _, _ = _build_http_engine(api, clock=clock)
        ref = engine.submit_run(request(), context("unknown-submit"))
        engine.wake()
        assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.RUNNING
        assert api.job_count == 1

        clock.advance(2)
        engine.wake()
        assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
        assert api.job_count == 1
        submit_requests = [item for item in api.requests if item["method"] == "POST" and item["path"] == "/jobs"]
        assert len(submit_requests) == 2
        assert submit_requests[0]["body"]["idempotency_key"] == submit_requests[1]["body"]["idempotency_key"] == ref.run_id


def test_result_rate_limit_does_not_advance_cursor_until_retry_succeeds():
    scenario = ApiScenario(
        records=({"record_id": "r1", "payload": {"key": "one", "body": "v1"}},),
        result_script=(
            ResponsePlan(
                status_code=429,
                body={
                    "code": "RATE_LIMITED",
                    "message": "slow down",
                    "retryable": True,
                    "retry_after": 2,
                },
            ),
        ),
        auth_token="test-secret",
    )
    clock = ManualClock()
    with FakeUnifiedApi(scenario) as api:
        engine, _, event_sink, _ = _build_http_engine(api, clock=clock)
        ref = engine.submit_run(request(), context("rate-limit"))
        engine.wake()
        first = engine.get_run(ref.run_id, "scope-a")
        assert first.status is RunStatus.RUNNING
        assert first.processed_count == 0
        assert len(event_sink.events) == 0

        clock.advance(2)
        engine.wake()
        assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
        assert len(event_sink.events) == 1
        assert len([item for item in api.requests if item["path"].endswith("/results")]) == 2


def test_http_result_replay_is_safe_for_history():
    scenario = ApiScenario(
        records=(
            {"record_id": "same", "observed_at": "2026-01-01T00:00:00Z", "payload": {"key": "one", "body": "v1"}},
            {"record_id": "same", "observed_at": "2026-01-01T00:00:00Z", "payload": {"key": "one", "body": "v1"}},
            {"record_id": "two", "observed_at": "2026-01-01T00:00:00Z", "payload": {"key": "two", "body": "v1"}},
        ),
        page_size=1,
        auth_token="test-secret",
    )
    clock = ManualClock()
    with FakeUnifiedApi(scenario) as api:
        engine, _, event_sink, _ = _build_http_engine(api, clock=clock)
        ref = engine.submit_run(request(), context("http-replay"))
        engine.wake()
        summary = engine.get_run(ref.run_id, "scope-a")
        assert summary.status is RunStatus.COMPLETED
        assert summary.processed_count == 2
        assert summary.failed_count == 0
        assert len(event_sink.events) == 2


def test_real_http_gateway_translates_auth_cancel_and_protocol_errors():
    scenario = ApiScenario(
        records=({"record_id": "r1", "payload": {"key": "one", "body": "v1"}},),
        result_script=(
            ResponsePlan(
                body={"records": {}, "has_more": False, "result_schema_version": "1.0"},
            ),
        ),
        auth_token="test-secret",
    )
    with FakeUnifiedApi(scenario) as api:
        unauthorized = UnifiedApiGateway(UnifiedApiConfig(api.base_url, "wrong-secret"), gateway_key="fake-http")
        with pytest.raises(UpstreamError) as caught:
            unauthorized.submit(UpstreamJobRequest(request().collection), "auth")
        assert caught.value.code == "UNAUTHORIZED"
        assert caught.value.retryable is False

        gateway = UnifiedApiGateway(UnifiedApiConfig(api.base_url, "test-secret"), gateway_key="fake-http")
        ref = gateway.submit(UpstreamJobRequest(request().collection), "cancel-me")
        cancelled = gateway.cancel(ref)
        assert cancelled.accepted is True
        assert cancelled.state is not None
        with pytest.raises(UpstreamError, match="records"):
            gateway.read_batch(ref, None)
