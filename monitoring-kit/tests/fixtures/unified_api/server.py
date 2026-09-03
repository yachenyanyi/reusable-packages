"""只在测试进程中运行的最小 HTTP 假统一采集 API。"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from urllib.parse import parse_qs, unquote, urlparse
from typing import Any

from .scenario import ApiScenario, ResponsePlan, json_time


class FakeUnifiedApi:
    """以真实 loopback HTTP 提供统一采集 API 的测试替身。"""

    def __init__(self, scenario: ApiScenario) -> None:
        self.scenario = scenario
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}
        self._requests: list[dict[str, Any]] = []
        self._submit_count = 0
        self._status_counts: dict[str, int] = {}
        self._result_counts: dict[str, int] = {}
        self._cancel_counts: dict[str, int] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("FakeUnifiedApi 尚未启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._requests)

    @property
    def job_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    @property
    def job_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._jobs)

    def start(self) -> "FakeUnifiedApi":
        if self._server is not None:
            raise RuntimeError("FakeUnifiedApi 不能重复启动")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "monitoring-kit-test-api/1.0"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol name
                owner._handle(self, "POST")

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler protocol name
                owner._handle(self, "GET")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-unified-api", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> "FakeUnifiedApi":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urlparse(handler.path)
        body = self._read_body(handler)
        with self._lock:
            self._requests.append(
                {
                    "method": method,
                    "path": parsed.path,
                    "query": parse_qs(parsed.query, keep_blank_values=True),
                    "body": body,
                    "headers": {key.lower(): value for key, value in handler.headers.items()},
                }
            )
        if not self._authorized(handler):
            self._send(handler, 401, {"code": "UNAUTHORIZED", "message": "invalid test credential", "retryable": False})
            return
        try:
            if method == "POST" and parsed.path == "/jobs":
                self._submit(handler, body)
                return
            parts = [unquote(part) for part in parsed.path.split("/") if part]
            if len(parts) == 2 and parts[0] == "jobs" and method == "GET":
                self._status(handler, parts[1])
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "results" and method == "GET":
                self._results(handler, parts[1], parse_qs(parsed.query, keep_blank_values=True))
                return
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel" and method == "POST":
                self._cancel(handler, parts[1])
                return
            self._send_error(handler, 404, "NOT_FOUND", "unknown test endpoint")
        except ValueError as exc:
            self._send_error(handler, 400, "INVALID_REQUEST", str(exc))
        except Exception:
            self._send_error(handler, 500, "FAKE_API_FAILURE", "fake API internal failure", retryable=True)

    @staticmethod
    def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        raw_length = handler.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length == 0:
            return {}
        decoded = json.loads(handler.rfile.read(length).decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return decoded

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if self.scenario.auth_token is None:
            return True
        return handler.headers.get("Authorization") == f"Bearer {self.scenario.auth_token}"

    def _submit(self, handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> None:
        idempotency_key = body.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key 必须是非空字符串")
        header_key = handler.headers.get("Idempotency-Key")
        if header_key != idempotency_key:
            raise ValueError("Idempotency-Key 与请求体不一致")
        source_type = body.get("source_type")
        schema_version = body.get("schema_version")
        if not isinstance(source_type, str) or not isinstance(schema_version, str):
            raise ValueError("source_type 和 schema_version 必须存在")
        with self._lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id is not None:
                self._send(handler, 200, {"job_id": existing_id, "status": "queued"})
                return
            job_id = f"{self.scenario.job_id_prefix}-{len(self._jobs) + 1}"
            self._jobs[job_id] = {
                "source_type": source_type,
                "schema_version": schema_version,
                "created_at": datetime.now(UTC),
                "cancelled": False,
            }
            self._idempotency[idempotency_key] = job_id
            script = _script_item(self.scenario.submission_script, self._submit_count)
            self._submit_count += 1
        accepted_at = self._jobs[job_id]["created_at"].isoformat().replace("+00:00", "Z")
        self._planned_or_default(
            handler,
            script,
            {
                "job_id": job_id,
                "status": "queued",
                "accepted_at": accepted_at,
                "request_schema_version": self._jobs[job_id]["schema_version"],
            },
        )

    def _status(self, handler: BaseHTTPRequestHandler, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                self._send_error(handler, 404, "NOT_FOUND", "job not found")
                return
            count = self._status_counts.get(job_id, 0)
            self._status_counts[job_id] = count + 1
            script = _script_item(self.scenario.status_script, count)
            state = self.scenario.status_timeline[min(count, len(self.scenario.status_timeline) - 1)]
            if job["cancelled"]:
                state = "cancelled"
            now = datetime.now(UTC)
            default = {
                "job_id": job_id,
                "status": state,
                "produced_count": len(self.scenario.records),
                "failed_count": 0,
                "updated_at": now.isoformat().replace("+00:00", "Z"),
            }
            if state in {"completed", "completed_with_errors", "failed", "cancelled"}:
                default["finished_at"] = now.isoformat().replace("+00:00", "Z")
        self._planned_or_default(handler, script, default)

    def _results(self, handler: BaseHTTPRequestHandler, job_id: str, query: dict[str, list[str]]) -> None:
        with self._lock:
            if job_id not in self._jobs:
                self._send_error(handler, 404, "NOT_FOUND", "job not found")
                return
            count = self._result_counts.get(job_id, 0)
            self._result_counts[job_id] = count + 1
            script = _script_item(self.scenario.result_script, count)
            raw_cursor = query.get("cursor", [None])[0]
            try:
                offset = int(raw_cursor) if raw_cursor is not None else 0
            except ValueError as exc:
                raise ValueError("cursor 必须是整数") from exc
            if offset < 0 or offset > len(self.scenario.records):
                self._send_error(handler, 400, "CURSOR_INVALID", "cursor out of range")
                return
            end = min(offset + self.scenario.page_size, len(self.scenario.records))
            records = [
                self._record_payload(item, job_id, index)
                for index, item in enumerate(self.scenario.records[offset:end], start=offset + 1)
            ]
            default = {
                "records": records,
                "has_more": end < len(self.scenario.records),
                "next_cursor": str(end) if end < len(self.scenario.records) else None,
                "result_schema_version": self.scenario.result_schema_version,
            }
        self._planned_or_default(handler, script, default)

    def _cancel(self, handler: BaseHTTPRequestHandler, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                self._send_error(handler, 404, "NOT_FOUND", "job not found")
                return
            count = self._cancel_counts.get(job_id, 0)
            self._cancel_counts[job_id] = count + 1
            script = _script_item(self.scenario.cancellation_script, count)
            job["cancelled"] = True
            default = {"accepted": True, "status": "cancelled"}
        self._planned_or_default(handler, script, default)

    def _record_payload(self, item: MappingProxyType | dict[str, Any] | Any, job_id: str, sequence: int) -> dict[str, Any]:
        value = dict(item)
        observed_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=sequence)
        payload = {
            "record_id": str(value.get("record_id", f"record-{sequence}")),
            "source_type": self._jobs[job_id]["source_type"],
            "schema_version": self._jobs[job_id]["schema_version"],
            "observed_at": json_time(value.get("observed_at"), observed_at),
            "payload": value.get("payload", {}),
            "external_id": value.get("external_id"),
            "published_at": json_time(value["published_at"], observed_at) if value.get("published_at") else None,
            "deleted": value.get("deleted", False),
            "raw_ref": value.get("raw_ref", value.get("raw_artifact_ref")),
            "provenance": value.get("provenance", {}),
            "sequence": value.get("sequence", sequence),
        }
        return payload

    @staticmethod
    def _planned_or_default(handler: BaseHTTPRequestHandler, plan: ResponsePlan | None, default: dict[str, Any]) -> None:
        if plan is None:
            _send_json(handler, 200, default)
            return
        body = dict(plan.body) if plan.body is not None else default
        _send_json(handler, plan.status_code, body, plan.headers)

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status_code: int, body: dict[str, Any]) -> None:
        _send_json(handler, status_code, body)

    @staticmethod
    def _send_error(
        handler: BaseHTTPRequestHandler,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        _send_json(handler, status_code, {"code": code, "message": message, "retryable": retryable})


def _script_item(script: tuple[ResponsePlan, ...], index: int) -> ResponsePlan | None:
    return script[index] if index < len(script) else None


def _send_json(
    handler: BaseHTTPRequestHandler,
    status_code: int,
    body: dict[str, Any],
    headers: MappingProxyType | dict[str, str] | Any = (),
) -> None:
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Connection", "close")
    for key, value in dict(headers).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(raw)
