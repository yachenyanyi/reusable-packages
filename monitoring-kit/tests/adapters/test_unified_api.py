import json
from urllib.error import HTTPError

import pytest

from monitoring_kit.adapters.upstream.unified_api import UnifiedApiConfig, UnifiedApiGateway
from monitoring_kit.collection.model import UpstreamJobRequest, UpstreamJobRef, UpstreamState
from monitoring_kit.collection.ports import UpstreamError

from tests.support import request


class FakeResponse:
    def __init__(self, value):
        self.value = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.value


def test_unified_api_gateway_translates_task_status_results_and_cancel(monkeypatch):
    seen = []
    responses = {
        ("POST", "http://example.test/jobs"): {"job_id": "job-1", "status": "queued"},
        ("GET", "http://example.test/jobs/job-1"): {"job_id": "job-1", "status": "completed", "produced_count": 1},
        ("GET", "http://example.test/jobs/job-1/results"): {
            "records": [
                {
                    "record_id": "record-1",
                    "source_type": "test.collection",
                    "schema_version": "1.0",
                    "observed_at": "2026-01-01T00:00:00Z",
                    "payload": {"key": "key", "body": "body"},
                }
            ],
            "has_more": False,
            "result_schema_version": "1.0",
        },
        ("POST", "http://example.test/jobs/job-1/cancel"): {"accepted": True, "status": "cancelled"},
    }

    def fake_urlopen(req, timeout):
        seen.append((req.method, req.full_url, req.headers, json.loads(req.data.decode()) if req.data else None, timeout))
        return FakeResponse(responses[(req.method, req.full_url)])

    monkeypatch.setattr("monitoring_kit.adapters.upstream.unified_api.urlopen", fake_urlopen)
    gateway = UnifiedApiGateway(UnifiedApiConfig("http://example.test", "secret"), gateway_key="api")
    ref = gateway.submit(UpstreamJobRequest(request().collection), "run-1")
    status = gateway.get_status(ref)
    batch = gateway.read_batch(ref, None)
    cancelled = gateway.cancel(ref)
    assert ref.job_id == "job-1"
    assert status.state is UpstreamState.COMPLETED
    assert batch.records[0].record_id == "record-1"
    assert cancelled.accepted is True
    assert seen[0][2]["Authorization"] == "Bearer secret"
    assert seen[0][3]["idempotency_key"] == "run-1"


def test_unified_api_maps_rate_limit_to_retryable_error(monkeypatch):
    def fake_urlopen(req, timeout):
        raise HTTPError(
            req.full_url,
            429,
            "rate limited",
            {"Retry-After": "3"},
            __import__("io").BytesIO(json.dumps({"code": "RATE_LIMITED", "message": "slow down"}).encode()),
        )

    monkeypatch.setattr("monitoring_kit.adapters.upstream.unified_api.urlopen", fake_urlopen)
    gateway = UnifiedApiGateway(UnifiedApiConfig("http://example.test"))
    with pytest.raises(UpstreamError) as caught:
        gateway.submit(UpstreamJobRequest(request().collection), "run-1")
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 3
    assert caught.value.submission_unknown is False
    assert caught.value.code == "RATE_LIMITED"
