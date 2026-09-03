"""上游网关回退路由。

路由只处理“选择哪个协议实现”和提交幂等缓存；运行重试、退避和游标推进
仍由 CollectionEngine 统一协调。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from ...collection.model import (
    UpstreamBatch,
    UpstreamCancellation,
    UpstreamJobRef,
    UpstreamJobRequest,
    UpstreamStatus,
)
from ...collection.ports import UpstreamError, UpstreamJobGateway


class GatewayRouter(UpstreamJobGateway):
    """把多个同一协议的网关组合成一个可回退网关。"""

    gateway_key = "gateway-router"

    def __init__(self, gateways: Sequence[UpstreamJobGateway]) -> None:
        if not gateways:
            raise ValueError("GatewayRouter 至少需要一个网关")
        self._gateways = tuple(gateways)
        self._by_key: dict[str, UpstreamJobGateway] = {}
        for gateway in self._gateways:
            key = getattr(gateway, "gateway_key", "")
            if not key or key in self._by_key:
                raise ValueError("网关必须有唯一的 gateway_key")
            self._by_key[key] = gateway
        self._submissions: dict[str, UpstreamJobRef] = {}
        self._routes: dict[str, int] = {}
        self._lock = threading.RLock()

    def submit(self, request: UpstreamJobRequest, idempotency_key: str) -> UpstreamJobRef:
        with self._lock:
            cached = self._submissions.get(idempotency_key)
            if cached is not None:
                return cached
            candidates = self._candidate_indexes(request)
            route_index = self._routes.get(idempotency_key, 0)

        last_error: UpstreamError | None = None
        for position in range(route_index, len(candidates)):
            index = candidates[position]
            gateway = self._gateways[index]
            try:
                ref = gateway.submit(request, idempotency_key)
            except UpstreamError as exc:
                last_error = exc
                with self._lock:
                    self._routes[idempotency_key] = position
                # 响应不确定时只能在同一候选上重试，不能跨提供方制造重复任务。
                if exc.submission_unknown or not exc.fallback_allowed:
                    raise
                continue
            except Exception as exc:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", str(exc), retryable=True) from exc

            normalized = UpstreamJobRef(getattr(gateway, "gateway_key"), ref.job_id)
            with self._lock:
                self._submissions[idempotency_key] = normalized
                self._routes.pop(idempotency_key, None)
            return normalized

        raise last_error or UpstreamError("NO_UPSTREAM_GATEWAY", "没有可用的上游网关")

    def get_status(self, job_ref: UpstreamJobRef) -> UpstreamStatus:
        return self._gateway(job_ref).get_status(job_ref)

    def read_batch(self, job_ref: UpstreamJobRef, cursor: str | None) -> UpstreamBatch:
        return self._gateway(job_ref).read_batch(job_ref, cursor)

    def cancel(self, job_ref: UpstreamJobRef) -> UpstreamCancellation:
        return self._gateway(job_ref).cancel(job_ref)

    def _gateway(self, job_ref: UpstreamJobRef) -> UpstreamJobGateway:
        try:
            return self._by_key[job_ref.gateway_key]
        except KeyError as exc:
            raise UpstreamError("UNKNOWN_GATEWAY", f"未知网关: {job_ref.gateway_key}") from exc

    def _candidate_indexes(self, request: UpstreamJobRequest) -> tuple[int, ...]:
        if request.gateway_hint is None:
            return tuple(range(len(self._gateways)))
        try:
            preferred = next(
                index
                for index, gateway in enumerate(self._gateways)
                if getattr(gateway, "gateway_key", None) == request.gateway_hint
            )
        except StopIteration as exc:
            raise UpstreamError("UNKNOWN_GATEWAY", f"未知网关: {request.gateway_hint}") from exc
        return (preferred,) + tuple(index for index in range(len(self._gateways)) if index != preferred)
