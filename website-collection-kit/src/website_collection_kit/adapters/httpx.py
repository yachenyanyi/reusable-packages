"""HTTPX acquisition adapter."""

from __future__ import annotations

import time

from ..errors import AcquisitionError, NoAcquisitionCapabilityError
from ..network import PublicNetworkPolicy
from ..ports import FetchRequest, FetchResponse


class HttpxAcquisition:
    """Fetch public resources without following redirects across the core scope.

    Redirects are returned to the core as data so the core can re-check the
    destination before making the next request.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = 8_000_000,
        user_agent: str = "website-collection-kit/0.1",
        trust_env: bool = False,
        network_policy: PublicNetworkPolicy | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self.trust_env = trust_env
        self.network_policy = network_policy or PublicNetworkPolicy()
        self._client = None

    async def _ensure_client(self):
        try:
            import httpx
        except ImportError as exc:
            raise NoAcquisitionCapabilityError(
                "HttpxAcquisition requires the 'http' extra"
            ) from exc
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=self.trust_env,
                timeout=httpx.Timeout(self.timeout_seconds),
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        return self._client

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if request.rendering.value == "required":
            raise AcquisitionError(
                "rendering_required",
                "static HTTP adapter cannot satisfy rendered acquisition",
            )
        decision = await self.network_policy.check_url(
            request.url,
            method="GET",
            allow_private_network=bool(
                request.scope and request.scope.allow_private_network
            ),
        )
        if not decision.accepted:
            raise AcquisitionError(
                "network_policy_denied",
                f"network policy denied {request.url}: {decision.reason}",
                retryable=decision.reason == "dns_resolution_failed",
            )
        client = await self._ensure_client()
        started = time.perf_counter()
        try:
            async with client.stream("GET", request.url) as response:
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_response_bytes:
                    raise AcquisitionError(
                        "response_too_large", "response exceeds configured size limit"
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise AcquisitionError(
                            "response_too_large",
                            "response exceeds configured size limit",
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                response_url = str(response.url)
                response_status = response.status_code
                response_headers = dict(response.headers)
        except AcquisitionError:
            raise
        except Exception as exc:
            raise AcquisitionError(
                "http_fetch_failed", str(exc), retryable=True
            ) from exc
        if len(body) > self.max_response_bytes:
            raise AcquisitionError(
                "response_too_large", "response exceeds configured size limit"
            )
        location = (
            response_headers.get("location") if 300 <= response_status < 400 else None
        )
        return FetchResponse(
            requested_url=request.url,
            final_url=response_url,
            status=response_status,
            headers=response_headers,
            body=body,
            media_type=response_headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower(),
            fetch_method="http",
            elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
            redirect_to=location,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
