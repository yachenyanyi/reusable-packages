"""Public-network and read-only request policy shared by acquisition adapters."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit


_PRIVATE_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)


@dataclass(frozen=True, slots=True)
class NetworkDecision:
    accepted: bool
    reason: str = ""


def literal_address_reason(host: str) -> str | None:
    """Return a stable rejection reason for a literal non-public address."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_global:
        return "private_network"
    return None


class PublicNetworkPolicy:
    """Allow public, read-only HTTP(S) traffic and reject private destinations.

    The policy is deliberately independent of page crawl scope.  A browser may
    load a public CDN resource without making that host a crawl candidate.  A
    deployment that must defend against DNS rebinding still needs controlled
    DNS and egress enforcement in its network boundary; this class provides the
    adapter-side preflight and repeated request checks.
    """

    def __init__(
        self,
        *,
        allow_private_network: bool = False,
        allowed_methods: tuple[str, ...] = ("GET", "HEAD"),
        dns_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(allow_private_network, bool):
            raise ValueError("allow_private_network must be a boolean")
        methods = tuple(dict.fromkeys(method.upper().strip() for method in allowed_methods))
        if not methods or any(not method or not method.isalpha() for method in methods):
            raise ValueError("allowed_methods must contain HTTP method names")
        if dns_timeout_seconds <= 0:
            raise ValueError("dns_timeout_seconds must be positive")
        self.allow_private_network = allow_private_network
        self.allowed_methods = methods
        self.dns_timeout_seconds = dns_timeout_seconds
        self._dns_cache: dict[str, NetworkDecision] = {}

    def decide_method(self, method: str) -> NetworkDecision:
        value = method.upper().strip() if isinstance(method, str) else ""
        if value not in self.allowed_methods:
            return NetworkDecision(False, "http_method_not_allowed")
        return NetworkDecision(True)

    async def check_url(
        self,
        url: str,
        *,
        method: str = "GET",
        allow_private_network: bool = False,
    ) -> NetworkDecision:
        method_decision = self.decide_method(method)
        if not method_decision.accepted:
            return method_decision
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            return NetworkDecision(False, "invalid_network_url")
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not host:
            return NetworkDecision(False, "network_scheme_not_allowed")
        if parsed.username or parsed.password:
            return NetworkDecision(False, "network_credentials_not_allowed")
        if port is not None and not 0 <= port <= 65535:
            return NetworkDecision(False, "invalid_network_port")
        if allow_private_network or self.allow_private_network:
            return NetworkDecision(True)

        host = host.lower().rstrip(".")
        if host in _PRIVATE_HOSTNAMES:
            return NetworkDecision(False, "private_network")
        literal_reason = literal_address_reason(host)
        if literal_reason:
            return NetworkDecision(False, literal_reason)
        return await self._check_resolved_host(host, port)

    async def _check_resolved_host(self, host: str, port: int | None) -> NetworkDecision:
        cache_key = host
        cached = self._dns_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port or 443,
                    socket.AF_UNSPEC,
                    socket.SOCK_STREAM,
                ),
                timeout=self.dns_timeout_seconds,
            )
        except (OSError, TimeoutError):
            decision = NetworkDecision(False, "dns_resolution_failed")
            self._dns_cache[cache_key] = decision
            return decision
        addresses = {record[4][0] for record in records if record[4]}
        if not addresses:
            decision = NetworkDecision(False, "dns_resolution_failed")
        else:
            reason = next(
                (literal_address_reason(address) for address in addresses if literal_address_reason(address)),
                None,
            )
            decision = NetworkDecision(True) if reason is None else NetworkDecision(False, reason)
        self._dns_cache[cache_key] = decision
        return decision
