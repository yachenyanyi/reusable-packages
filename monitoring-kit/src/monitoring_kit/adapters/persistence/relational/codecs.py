"""领域对象与关系型适配器 JSON 载荷之间的私有编码。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ....collection.model import Attempt, RecordFailure, RunRecord, UpstreamJobRef, UpstreamJobRequest
from ....contracts.change import ChangeEvent, Document, Snapshot
from ....contracts.envelope import TypedEnvelope
from ....contracts.primitives import parse_datetime, stable_json
from ....contracts.run import RunError, RunRequest, RunStatus, ExecutionContext
from ....contracts.observation import Observation, SubjectRef
from ....history.model import HistoryResult
from .errors import PersistenceInvariantError


def json_dumps(value: Any) -> str:
    return stable_json(value)


def json_loads(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    return decoded


def key_hash(*parts: str) -> str:
    return hashlib.sha256(stable_json(list(parts)).encode("utf-8")).hexdigest()


def scope_hash(scope_key: str) -> str:
    return key_hash(scope_key)


def idempotency_hash(scope_key: str, idempotency_key: str) -> str:
    return key_hash(scope_key, idempotency_key)


def ingest_hash(scope_key: str, gateway_key: str, upstream_record_id: str) -> str:
    return key_hash(scope_key, gateway_key, upstream_record_id)


def subject_hash(subject: SubjectRef) -> str:
    return key_hash(subject.namespace, subject.key, subject.identity_version)


def encode_run(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "scope_key": record.scope_key,
        "request": record.request.to_dict(),
        "context": record.context.to_dict(),
        "request_fingerprint": record.request_fingerprint,
        "adapter_key": record.adapter_key,
        "upstream_request": {
            "collection": record.upstream_request.collection.to_dict(),
            "source_ref": record.upstream_request.source_ref,
            "client_metadata": dict(record.upstream_request.client_metadata),
            "gateway_hint": record.upstream_request.gateway_hint,
        },
        "status": record.status.value,
        "accepted_at": record.accepted_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "upstream_job": (
            {"gateway_key": record.upstream_job.gateway_key, "job_id": record.upstream_job.job_id}
            if record.upstream_job
            else None
        ),
        "cursor": record.cursor,
        "processed_count": record.processed_count,
        "failed_count": record.failed_count,
        "change_count": record.change_count,
        "consecutive_failures": record.consecutive_failures,
        "next_wakeup_at": record.next_wakeup_at.isoformat() if record.next_wakeup_at else None,
        "lease_owner": record.lease_owner,
        "lease_until": record.lease_until.isoformat() if record.lease_until else None,
        "cancel_requested": record.cancel_requested,
        "error": record.error.to_dict() if record.error else None,
        "attempts": [_encode_attempt(item) for item in record.attempts],
        "processed_ingest_keys": [list(item) for item in sorted(record.processed_ingest_keys)],
        "record_failures": [_encode_record_failure(item) for item in record.record_failures],
    }


def decode_run(value: str | dict[str, Any]) -> RunRecord:
    return _decode_contract(value, "Run", _decode_run)


def _decode_run(value: str | dict[str, Any]) -> RunRecord:
    raw = json_loads(value, "Run") if isinstance(value, str) else value
    upstream_request = raw["upstream_request"]
    upstream_job = raw.get("upstream_job")
    error = raw.get("error")
    return RunRecord(
        run_id=raw["run_id"],
        scope_key=raw["scope_key"],
        request=RunRequest.from_dict(raw["request"]),
        context=ExecutionContext.from_dict(raw["context"]),
        request_fingerprint=raw["request_fingerprint"],
        adapter_key=raw["adapter_key"],
        upstream_request=UpstreamJobRequest(
            collection=TypedEnvelope.from_dict(upstream_request["collection"]),
            source_ref=upstream_request.get("source_ref"),
            client_metadata=upstream_request.get("client_metadata", {}),
            gateway_hint=upstream_request.get("gateway_hint"),
        ),
        status=RunStatus(raw["status"]),
        accepted_at=parse_datetime(raw["accepted_at"], "accepted_at"),
        updated_at=parse_datetime(raw["updated_at"], "updated_at"),
        started_at=_optional_datetime(raw.get("started_at"), "started_at"),
        finished_at=_optional_datetime(raw.get("finished_at"), "finished_at"),
        upstream_job=UpstreamJobRef(upstream_job["gateway_key"], upstream_job["job_id"])
        if upstream_job
        else None,
        cursor=raw.get("cursor"),
        processed_count=raw.get("processed_count", 0),
        failed_count=raw.get("failed_count", 0),
        change_count=raw.get("change_count", 0),
        consecutive_failures=raw.get("consecutive_failures", 0),
        next_wakeup_at=_optional_datetime(raw.get("next_wakeup_at"), "next_wakeup_at"),
        lease_owner=raw.get("lease_owner"),
        lease_until=_optional_datetime(raw.get("lease_until"), "lease_until"),
        cancel_requested=raw.get("cancel_requested", False),
        error=RunError.from_dict(error) if error else None,
        attempts=[_decode_attempt(item) for item in raw.get("attempts", [])],
        processed_ingest_keys={tuple(item) for item in raw.get("processed_ingest_keys", [])},
        record_failures=[_decode_record_failure(item) for item in raw.get("record_failures", [])],
    )


def _encode_attempt(value: Attempt) -> dict[str, Any]:
    return {
        "attempt_id": value.attempt_id,
        "run_id": value.run_id,
        "operation": value.operation,
        "gateway_key": value.gateway_key,
        "started_at": value.started_at.isoformat(),
        "finished_at": value.finished_at.isoformat(),
        "outcome": value.outcome,
        "error_code": value.error_code,
    }


def _decode_attempt(value: dict[str, Any]) -> Attempt:
    return Attempt(
        attempt_id=value["attempt_id"],
        run_id=value["run_id"],
        operation=value["operation"],
        gateway_key=value.get("gateway_key"),
        started_at=parse_datetime(value["started_at"], "attempt.started_at"),
        finished_at=parse_datetime(value["finished_at"], "attempt.finished_at"),
        outcome=value["outcome"],
        error_code=value.get("error_code"),
    )


def _encode_record_failure(value: RecordFailure) -> dict[str, str]:
    return {
        "upstream_record_id": value.upstream_record_id,
        "code": value.code,
        "message": value.message,
    }


def _decode_record_failure(value: dict[str, Any]) -> RecordFailure:
    return RecordFailure(value["upstream_record_id"], value["code"], value["message"])


def _optional_datetime(value: str | None, field_name: str) -> datetime | None:
    return parse_datetime(value, field_name) if value else None


def encode_history_result(result: HistoryResult) -> dict[str, Any]:
    return {
        "observation": result.observation.to_dict(),
        "document": result.document.to_dict(),
        "snapshot": result.snapshot.to_dict() if result.snapshot else None,
        "events": [event.to_dict() for event in result.events],
        "duplicate": result.duplicate,
    }


def decode_history_result(value: str | dict[str, Any]) -> HistoryResult:
    return _decode_contract(value, "HistoryResult", _decode_history_result)


def _decode_history_result(value: str | dict[str, Any]) -> HistoryResult:
    raw = json_loads(value, "HistoryResult") if isinstance(value, str) else value
    return HistoryResult(
        observation=Observation.from_dict(raw["observation"]),
        document=Document.from_dict(raw["document"]),
        snapshot=Snapshot.from_dict(raw["snapshot"]) if raw.get("snapshot") else None,
        events=tuple(ChangeEvent.from_dict(item) for item in raw.get("events", [])),
        duplicate=bool(raw.get("duplicate", False)),
    )


def decode_document(value: str | dict[str, Any]) -> Document:
    return _decode_contract(value, "Document", Document.from_dict)


def decode_snapshot(value: str | dict[str, Any]) -> Snapshot:
    return _decode_contract(value, "Snapshot", Snapshot.from_dict)


def decode_event(value: str | dict[str, Any]) -> ChangeEvent:
    return _decode_contract(value, "ChangeEvent", ChangeEvent.from_dict)


def _decode_contract(
    value: str | dict[str, Any],
    label: str,
    decoder: Callable[[dict[str, Any]], Any],
) -> Any:
    try:
        raw = json_loads(value, label) if isinstance(value, str) else value
        return decoder(raw)
    except PersistenceInvariantError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise PersistenceInvariantError(f"{label} 持久化载荷无效") from exc
