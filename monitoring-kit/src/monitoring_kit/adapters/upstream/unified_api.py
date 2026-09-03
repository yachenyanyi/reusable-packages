"""统一采集 API 的标准库 HTTP 适配器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ...collection.model import (
    UpstreamBatch,
    UpstreamCancellation,
    UpstreamErrorInfo,
    UpstreamJobRef,
    UpstreamJobRequest,
    UpstreamRecord,
    UpstreamState,
    UpstreamStatus,
)
from ...collection.ports import UpstreamError, UpstreamJobGateway
from ...contracts.primitives import parse_datetime, require_text


@dataclass(frozen=True, slots=True)
class UnifiedApiConfig:
    base_url: str
    api_token: str | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        base = self.base_url.rstrip("/")
        if not (base.startswith("http://") or base.startswith("https://")):
            raise ValueError("统一采集 API base_url 必须使用 http 或 https")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        object.__setattr__(self, "base_url", base)


class UnifiedApiGateway(UpstreamJobGateway):
    """按统一采集 API 接入前提翻译 HTTP 请求和稳定结果。"""

    def __init__(self, config: UnifiedApiConfig, *, gateway_key: str = "unified-api") -> None:
        self.config = config
        self.gateway_key = require_text(gateway_key, "gateway_key")

    def submit(self, request: UpstreamJobRequest, idempotency_key: str) -> UpstreamJobRef:
        body = {
            "idempotency_key": idempotency_key,
            "source_type": request.collection.type_key,
            "task_spec": dict(request.collection.data),
            "schema_version": request.collection.schema_version,
            "client_metadata": dict(request.client_metadata),
        }
        response = self._request(
            "POST",
            "/jobs",
            body=body,
            headers={"Idempotency-Key": idempotency_key},
            submission_unknown=True,
        )
        job_id = _text_field(response, "job_id")
        return UpstreamJobRef(self.gateway_key, job_id)

    def get_status(self, job_ref: UpstreamJobRef) -> UpstreamStatus:
        response = self._request("GET", f"/jobs/{_path_part(job_ref.job_id)}")
        state = _state(response.get("status"))
        error = _error_info(response.get("error"))
        return UpstreamStatus(
            job_ref=job_ref,
            state=state,
            produced_count=_nonnegative_int(response.get("produced_count", 0), "produced_count"),
            failed_count=_nonnegative_int(response.get("failed_count", 0), "failed_count"),
            updated_at=_optional_time(response.get("updated_at"), "updated_at"),
            finished_at=_optional_time(response.get("finished_at"), "finished_at"),
            error=error,
            latest_cursor=_optional_text(response.get("latest_cursor")),
        )

    def read_batch(self, job_ref: UpstreamJobRef, cursor: str | None) -> UpstreamBatch:
        query = {"cursor": cursor} if cursor is not None else {}
        path = f"/jobs/{_path_part(job_ref.job_id)}/results"
        if query:
            path += "?" + urlencode(query)
        response = self._request("GET", path)
        raw_records = response.get("records", [])
        if not isinstance(raw_records, list):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "results.records 必须是数组")
        records = tuple(_record(item, job_ref) for item in raw_records)
        has_more = response.get("has_more")
        if not isinstance(has_more, bool):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "results.has_more 必须是布尔值")
        next_cursor = _optional_text(response.get("next_cursor"))
        if has_more and not next_cursor:
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "has_more=true 但缺少 next_cursor")
        schema_version = _text_field(response, "result_schema_version", default="1.0")
        return UpstreamBatch(records, next_cursor, has_more, schema_version)

    def cancel(self, job_ref: UpstreamJobRef) -> UpstreamCancellation:
        response = self._request("POST", f"/jobs/{_path_part(job_ref.job_id)}/cancel")
        accepted = response.get("accepted", True)
        if not isinstance(accepted, bool):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "cancel.accepted 必须是布尔值")
        return UpstreamCancellation(accepted, _state(response.get("status", "cancelled")))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        submission_unknown: bool = False,
    ) -> dict:
        request_headers = {"Accept": "application/json"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if self.config.api_token:
            request_headers["Authorization"] = f"Bearer {self.config.api_token}"
        if headers:
            request_headers.update(headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.config.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            raise self._http_error(exc, submission_unknown=submission_unknown) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpstreamError(
                "UPSTREAM_NETWORK",
                "无法连接统一采集 API",
                retryable=True,
                submission_unknown=submission_unknown,
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "统一采集 API 返回了无效 JSON") from exc
        if not isinstance(decoded, dict):
            raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "统一采集 API 返回值必须是对象")
        return decoded

    @staticmethod
    def _http_error(exc: HTTPError, *, submission_unknown: bool) -> UpstreamError:
        status = exc.code
        body: dict = {}
        try:
            decoded = json.loads(exc.read().decode("utf-8"))
            if isinstance(decoded, dict):
                body = decoded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        code = _optional_text(body.get("code")) or _http_code(status)
        message = _optional_text(body.get("message")) or f"统一采集 API 返回 HTTP {status}"
        retryable = body.get("retryable")
        if not isinstance(retryable, bool):
            retryable = status in {408, 425, 429, 500, 502, 503, 504}
        retry_after = body.get("retry_after")
        if not isinstance(retry_after, (int, float)) or retry_after < 0:
            retry_after = None
            header_retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                parsed_header = float(header_retry_after) if header_retry_after is not None else None
            except (TypeError, ValueError):
                parsed_header = None
            if parsed_header is not None and parsed_header >= 0:
                retry_after = parsed_header
        fallback_allowed = status in {429, 500, 502, 503, 504}
        return UpstreamError(
            code,
            message,
            retryable=retryable,
            retry_after_seconds=float(retry_after) if retry_after is not None else None,
            fallback_allowed=fallback_allowed,
            submission_unknown=submission_unknown and status >= 500,
        )


def _path_part(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _text_field(value: dict, field_name: str, default: str | None = None) -> str:
    result = value.get(field_name, default)
    if not isinstance(result, str) or not result.strip():
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", f"缺少有效字段: {field_name}")
    return result.strip()


def _optional_text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _state(value) -> UpstreamState:
    try:
        return UpstreamState(str(value))
    except ValueError as exc:
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", f"未知上游任务状态: {value}") from exc


def _nonnegative_int(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", f"{field_name} 必须是非负整数")
    return value


def _optional_time(value, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_datetime(value, field_name)
    except ValueError as exc:
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", str(exc)) from exc


def _error_info(value) -> UpstreamErrorInfo | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "error 必须是对象")
    code = _text_field(value, "code", default="UPSTREAM_JOB_FAILED")
    message = _text_field(value, "message", default=code)
    retryable = value.get("retryable", False)
    if not isinstance(retryable, bool):
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "error.retryable 必须是布尔值")
    retry_after = value.get("retry_after")
    if retry_after is not None and (not isinstance(retry_after, (int, float)) or retry_after < 0):
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "error.retry_after 必须是非负数字")
    return UpstreamErrorInfo(code, message, retryable, float(retry_after) if retry_after is not None else None)


def _record(value, job_ref: UpstreamJobRef) -> UpstreamRecord:
    if not isinstance(value, dict):
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", "结果记录必须是对象")
    try:
        return UpstreamRecord(
            record_id=_text_field(value, "record_id"),
            job_ref=job_ref,
            source_type=_text_field(value, "source_type"),
            schema_version=_text_field(value, "schema_version"),
            observed_at=parse_datetime(value["observed_at"], "observed_at"),
            payload=value.get("payload", {}),
            external_id=_optional_text(value.get("external_id")),
            published_at=_optional_time(value.get("published_at"), "published_at"),
            deleted=value.get("deleted", False),
            raw_artifact_ref=_optional_text(value.get("raw_ref")),
            provenance=value.get("provenance", {}),
            sequence=value.get("sequence"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise UpstreamError("UPSTREAM_PROTOCOL_ERROR", f"结果记录字段无效: {exc}") from exc


def _http_code(status: int) -> str:
    return {
        401: "UPSTREAM_UNAUTHORIZED",
        403: "UPSTREAM_FORBIDDEN",
        404: "UPSTREAM_NOT_FOUND",
        409: "UPSTREAM_IDEMPOTENCY_CONFLICT",
        429: "UPSTREAM_RATE_LIMITED",
    }.get(status, f"UPSTREAM_HTTP_{status}")
