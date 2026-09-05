"""URL identity and scope enforcement.

This module owns all URL decisions. Callers should not reproduce its
normalisation or path-boundary rules.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import (
    parse_qsl,
    quote,
    quote_plus,
    unquote_plus,
    urljoin,
    urlsplit,
    urlunsplit,
)

from .contracts import Scope
from .network import literal_address_reason


@dataclass(frozen=True, slots=True)
class UrlDecision:
    accepted: bool
    canonical_url: str
    reason: str = ""
    request_url: str = ""


class UrlPolicy:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self._excluded = tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in scope.excluded_path_patterns
        )

    def resolve(self, raw_url: str, base_url: str | None = None) -> str:
        if not isinstance(raw_url, str):
            return ""
        raw_url = raw_url.strip()
        if base_url:
            try:
                raw_url = urljoin(base_url, raw_url)
            except ValueError:
                return ""
        return raw_url

    def request_url(self, raw_url: str, base_url: str | None = None) -> str:
        """Return a wire URL without applying identity-only normalization."""

        resolved = self.resolve(raw_url, base_url)
        try:
            parsed = urlsplit(resolved)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                return ""
            parsed.port
        except (TypeError, ValueError):
            return ""
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

    def canonicalize(self, raw_url: str, base_url: str | None = None) -> str:
        resolved = self.resolve(raw_url, base_url)
        try:
            parsed = urlsplit(resolved)
        except ValueError:
            return ""
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower().rstrip(".")
        try:
            hostname = hostname.encode("idna").decode("ascii")
            port = parsed.port
        except (UnicodeError, ValueError):
            return ""
        if parsed.username or parsed.password:
            return ""
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            netloc = f"{netloc}:{port}"

        path = parsed.path or "/"
        path = re.sub(r"/{2,}", "/", path)
        path = posixpath.normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        if parsed.path.endswith("/") and not path.endswith("/"):
            path += "/"
        path = quote(path, safe="/%:@!$&'()*+,;=-._~")

        query_pairs = []
        ignored = set(self.scope.ignored_query_keys)
        for key, value, has_equals in _query_parts(parsed.query):
            if key.lower() in ignored or key.lower().startswith("utm_"):
                continue
            # A key-only query is not always equivalent to a key with an empty
            # value.  Several server-rendered CMS routes use ``?<uuid>`` as
            # their page identity and return an empty shell for
            # ``?<uuid>=``.  Keep that wire-level distinction while retaining
            # the existing deterministic ordering.
            query_pairs.append((key, value, has_equals))
        query_pairs.sort()
        query = "&".join(
            _encode_query_part(key, value, has_equals)
            for key, value, has_equals in query_pairs
        )
        return urlunsplit((scheme, netloc, path, query, ""))

    def decide(self, raw_url: str, base_url: str | None = None) -> UrlDecision:
        request_url = self.request_url(raw_url, base_url)
        canonical = self.canonicalize(raw_url, base_url)
        if not canonical:
            return UrlDecision(
                False, "", "unsupported_scheme_or_missing_host", request_url
            )
        if len(request_url) > self.scope.max_url_length or len(canonical) > self.scope.max_url_length:
            return UrlDecision(False, canonical, "url_too_long", request_url)
        try:
            parsed = urlsplit(canonical)
        except ValueError:
            return UrlDecision(
                False, canonical, "unsupported_scheme_or_missing_host", request_url
            )
        if parsed.scheme not in self.scope.allowed_schemes:
            return UrlDecision(False, canonical, "scheme_not_allowed", request_url)
        if not self.scope._host_allowed(parsed.hostname or ""):
            return UrlDecision(False, canonical, "external_host", request_url)
        if not self.scope._origin_allowed(
            parsed.scheme, parsed.hostname or "", parsed.port
        ):
            return UrlDecision(False, canonical, "external_origin", request_url)
        if (
            not self.scope.allow_private_network
            and literal_address_reason(parsed.hostname or "")
        ):
            return UrlDecision(False, canonical, "private_network", request_url)
        if self.scope.allowed_path_prefixes and not self._path_allowed(parsed.path):
            return UrlDecision(False, canonical, "path_outside_scope", request_url)
        if any(pattern.search(parsed.path) for pattern in self._excluded):
            return UrlDecision(False, canonical, "path_excluded", request_url)
        if self.scope.allowed_query_keys is not None:
            allowed = set(self.scope.allowed_query_keys)
            actual = {
                key.lower()
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            }
            if not actual.issubset(allowed):
                return UrlDecision(False, canonical, "query_key_not_allowed", request_url)
        return UrlDecision(True, canonical, request_url=request_url)

    def decide_auxiliary(
        self, raw_url: str, base_url: str | None = None
    ) -> UrlDecision:
        """Validate robots, sitemap, and feed URLs without widening page scope."""

        request_url = self.request_url(raw_url, base_url)
        canonical = self.canonicalize(raw_url, base_url)
        if not canonical:
            return UrlDecision(
                False, "", "unsupported_scheme_or_missing_host", request_url
            )
        if len(request_url) > self.scope.max_url_length or len(canonical) > self.scope.max_url_length:
            return UrlDecision(False, canonical, "url_too_long", request_url)
        try:
            parsed = urlsplit(canonical)
        except ValueError:
            return UrlDecision(
                False, canonical, "unsupported_scheme_or_missing_host", request_url
            )
        if parsed.scheme not in self.scope.allowed_schemes:
            return UrlDecision(False, canonical, "scheme_not_allowed", request_url)
        if not self.scope._host_allowed(parsed.hostname or ""):
            return UrlDecision(False, canonical, "external_host", request_url)
        if not self.scope._origin_allowed(
            parsed.scheme, parsed.hostname or "", parsed.port
        ):
            return UrlDecision(False, canonical, "external_origin", request_url)
        if (
            not self.scope.allow_private_network
            and literal_address_reason(parsed.hostname or "")
        ):
            return UrlDecision(False, canonical, "private_network", request_url)
        return UrlDecision(True, canonical, request_url=request_url)

    def _path_allowed(self, path: str) -> bool:
        for prefix in self.scope.allowed_path_prefixes:
            if (
                prefix == "/"
                or path == prefix
                or path.startswith(prefix.rstrip("/") + "/")
            ):
                return True
        return False

    def decisions_for(
        self, raw_urls: Iterable[str], base_url: str | None = None
    ) -> tuple[UrlDecision, ...]:
        return tuple(self.decide(raw_url, base_url) for raw_url in raw_urls)


def _query_parts(query: str) -> tuple[tuple[str, str, bool], ...]:
    """Parse a query while retaining whether each item had an equals sign."""

    result: list[tuple[str, str, bool]] = []
    for raw_part in query.split("&"):
        if not raw_part:
            continue
        if "=" in raw_part:
            raw_key, raw_value = raw_part.split("=", 1)
            has_equals = True
        else:
            raw_key, raw_value = raw_part, ""
            has_equals = False
        result.append((unquote_plus(raw_key), unquote_plus(raw_value), has_equals))
    return tuple(result)


def _encode_query_part(key: str, value: str, has_equals: bool) -> str:
    encoded_key = quote_plus(key, safe="")
    if not has_equals:
        return encoded_key
    return f"{encoded_key}={quote_plus(value, safe='')}"
