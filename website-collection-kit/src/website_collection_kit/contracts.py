"""Stable, transport-independent contracts for website collection.

The module deliberately contains no HTTP, browser, queue, database, or framework
types.  The same dataclasses are used by a direct Python caller and by the
unified API adapter after explicit request/response translation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

SCHEMA_DISCOVERY = "website.discovery@1.0"
SCHEMA_COLLECTION = "website.collection@1.0"
SCHEMA_PAGE = "website.page@1.0"


class PageType(str, Enum):
    HOME = "home"
    LIST = "list"
    NEWS_LIST = "news_list"
    NOTICE_LIST = "notice_list"
    ARTICLE = "article"
    NOTICE = "notice"
    PROFILE = "profile"
    RESEARCH = "research"
    PRODUCT = "product"
    RECRUITMENT = "recruitment"
    CONTACT = "contact"
    ATTACHMENT = "attachment"
    SEARCH = "search"
    LOGIN = "login"
    OTHER = "other"


class CollectionIntent(str, Enum):
    SITE_SWEEP = "site_sweep"
    TARGETED_REFRESH = "targeted_refresh"


class CollectionStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


class StopReason(str, Enum):
    CONVERGED = "converged"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RenderingRequirement(str, Enum):
    STATIC = "static"
    PREFERRED = "preferred"
    REQUIRED = "required"


class DiscoverySource(str, Enum):
    EXPLICIT_SEED = "explicit_seed"
    KNOWN_URL = "known_url"
    ROBOTS = "robots"
    SITEMAP = "sitemap"
    RSS = "rss"
    HTML_LINK = "html_link"
    DOM_LINK = "dom_link"
    DATA_URL = "data_url"
    SCRIPT_URL = "script_url"
    EMBEDDED_URL = "embedded_url"
    META_REFRESH = "meta_refresh"
    ONCLICK = "onclick"
    REDIRECT = "redirect"
    PROFILE_HINT = "profile_hint"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_non_empty(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalise_host(value: str) -> str:
    value = _require_non_empty("host", value).lower().rstrip(".")
    if "://" in value or "/" in value or "@" in value:
        raise ValueError("allowed_hosts must contain host names, not URLs")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid host: {value}") from exc


def _normalise_prefix(value: str) -> str:
    value = _require_non_empty("path prefix", value)
    if not value.startswith("/"):
        value = "/" + value
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def _host_is_same_or_below(host: str, base: str) -> bool:
    return host == base or host.endswith("." + base)


@dataclass(frozen=True, slots=True)
class Scope:
    """The maximum URL space an operation may visit.

    An empty ``allowed_path_prefixes`` means every path on the allowed host.
    Query parameters are retained for identity except for known tracking keys.
    ``allowed_query_keys`` can further narrow the accepted keys when a platform
    sub-site uses query parameters as part of its identity.
    """

    allowed_hosts: tuple[str, ...]
    allowed_path_prefixes: tuple[str, ...] = ()
    excluded_path_patterns: tuple[str, ...] = ()
    allowed_query_keys: tuple[str, ...] = ()
    ignored_query_keys: tuple[str, ...] = (
        "gclid",
        "fbclid",
        "msclkid",
        "yclid",
        "_ga",
        "_gl",
    )
    allowed_schemes: tuple[str, ...] = ("http", "https")
    allow_subdomains: bool = False
    respect_robots: bool = True
    max_url_length: int = 4096

    def __post_init__(self) -> None:
        hosts = tuple(
            dict.fromkeys(_normalise_host(host) for host in self.allowed_hosts)
        )
        if not hosts:
            raise ValueError("allowed_hosts must not be empty")
        prefixes = tuple(
            dict.fromkeys(
                _normalise_prefix(prefix) for prefix in self.allowed_path_prefixes
            )
        )
        schemes = tuple(
            dict.fromkeys(scheme.lower().strip() for scheme in self.allowed_schemes)
        )
        if not schemes or any(scheme not in {"http", "https"} for scheme in schemes):
            raise ValueError("allowed_schemes may only contain http and https")
        if self.max_url_length < 256:
            raise ValueError("max_url_length must be at least 256")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_path_prefixes", prefixes)
        object.__setattr__(self, "allowed_schemes", schemes)
        object.__setattr__(
            self,
            "allowed_query_keys",
            tuple(dict.fromkeys(k.strip().lower() for k in self.allowed_query_keys)),
        )
        object.__setattr__(
            self,
            "ignored_query_keys",
            tuple(dict.fromkeys(k.strip().lower() for k in self.ignored_query_keys)),
        )

    @classmethod
    def for_seeds(
        cls,
        seeds: Sequence[str],
        *,
        allowed_path_prefixes: Sequence[str] = (),
        **kwargs: Any,
    ) -> Scope:
        if isinstance(seeds, (str, bytes)):
            raise TypeError("seeds must be a sequence of URLs")
        hosts: list[str] = []
        for seed in seeds:
            if not isinstance(seed, str):
                raise TypeError("seed must be a URL string")
            try:
                parsed = urlsplit(seed)
                hostname = parsed.hostname
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"invalid seed: {seed}") from exc
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not hostname
                or port is not None
                and not 0 <= port <= 65535
            ):
                raise ValueError(f"seed has no host: {seed}")
            hosts.append(hostname)
        return cls(
            allowed_hosts=tuple(dict.fromkeys(hosts)),
            allowed_path_prefixes=tuple(allowed_path_prefixes),
            **kwargs,
        )

    def restrict_with(self, other: Scope) -> Scope:
        """Return the intersection of two scopes without widening either one."""

        # A scope has one subdomain flag for all of its hosts, so calculate the
        # representable intersection explicitly instead of only checking the
        # left-hand host list.  In particular, ``example.org/*`` intersected
        # with an exact ``research.example.org`` profile is valid.
        hosts: list[str] = []
        for left in self.allowed_hosts:
            for right in other.allowed_hosts:
                if (
                    left == right
                    or _host_is_same_or_below(left, right)
                    and other.allow_subdomains
                ):
                    hosts.append(left)
                elif _host_is_same_or_below(right, left) and self.allow_subdomains:
                    hosts.append(right)
        hosts = list(dict.fromkeys(hosts))
        if not hosts:
            raise ValueError("profile scope does not overlap operation scope")

        prefixes: tuple[str, ...]
        if not self.allowed_path_prefixes:
            prefixes = other.allowed_path_prefixes
        elif not other.allowed_path_prefixes:
            prefixes = self.allowed_path_prefixes
        else:
            values: list[str] = []
            for left in self.allowed_path_prefixes:
                for right in other.allowed_path_prefixes:
                    if left == right or left.startswith(right.rstrip("/") + "/"):
                        values.append(left)
                    elif right.startswith(left.rstrip("/") + "/"):
                        values.append(right)
            prefixes = tuple(dict.fromkeys(values))
            if not prefixes:
                raise ValueError("profile path scope does not overlap operation scope")

        if self.allowed_query_keys and other.allowed_query_keys:
            query_keys = tuple(
                key
                for key in self.allowed_query_keys
                if key in other.allowed_query_keys
            )
        else:
            query_keys = self.allowed_query_keys or other.allowed_query_keys
        return Scope(
            allowed_hosts=tuple(hosts),
            allowed_path_prefixes=prefixes,
            excluded_path_patterns=tuple(
                dict.fromkeys(
                    self.excluded_path_patterns + other.excluded_path_patterns
                )
            ),
            allowed_query_keys=query_keys,
            ignored_query_keys=tuple(
                dict.fromkeys(self.ignored_query_keys + other.ignored_query_keys)
            ),
            allowed_schemes=tuple(
                scheme
                for scheme in self.allowed_schemes
                if scheme in other.allowed_schemes
            ),
            allow_subdomains=self.allow_subdomains and other.allow_subdomains,
            respect_robots=self.respect_robots or other.respect_robots,
            max_url_length=min(self.max_url_length, other.max_url_length),
        )

    def _host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(
            host == allowed or (self.allow_subdomains and host.endswith("." + allowed))
            for allowed in self.allowed_hosts
        )


@dataclass(frozen=True, slots=True)
class Budget:
    max_pages: int = 1000
    max_candidates: int = 5000
    max_depth: int = 8
    max_duration_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be positive")
        if self.max_candidates < self.max_pages:
            raise ValueError("max_candidates must be at least max_pages")
        if self.max_depth < 0:
            raise ValueError("max_depth must not be negative")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RoutePattern:
    pattern: str
    page_type: PageType
    label: str = ""

    def __post_init__(self) -> None:
        _require_non_empty("route pattern", self.pattern)
        re.compile(self.pattern)
        if not isinstance(self.page_type, PageType):
            try:
                object.__setattr__(self, "page_type", PageType(self.page_type))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"unsupported route page type: {self.page_type}"
                ) from exc


@dataclass(frozen=True, slots=True)
class FieldHints:
    """Small, validated selector hints; not an arbitrary selector dictionary."""

    title_selectors: tuple[str, ...] = ()
    body_selectors: tuple[str, ...] = ()
    section_selectors: tuple[str, ...] = ()
    published_selectors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SiteProfile:
    profile_id: str
    version: str
    site_ref: str
    seeds: tuple[str, ...] = ()
    scope: Scope | None = None
    route_patterns: tuple[RoutePattern, ...] = ()
    excluded_path_patterns: tuple[str, ...] = ()
    discovery_hints: tuple[str, ...] = ()
    rendering_required: bool = False
    rendering_fallback: bool = True
    field_hints: FieldHints = FieldHints()
    attachment_extensions: tuple[str, ...] = (
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".zip",
    )
    registered_strategy_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty("profile_id", self.profile_id)
        _require_non_empty("profile version", self.version)
        _require_non_empty("site_ref", self.site_ref)
        object.__setattr__(self, "seeds", tuple(dict.fromkeys(self.seeds)))
        object.__setattr__(
            self, "discovery_hints", tuple(dict.fromkeys(self.discovery_hints))
        )
        object.__setattr__(
            self,
            "attachment_extensions",
            tuple(
                ext.lower() if ext.startswith(".") else "." + ext.lower()
                for ext in self.attachment_extensions
            ),
        )
        for pattern in self.excluded_path_patterns:
            re.compile(pattern)


@dataclass(frozen=True, slots=True)
class InspectionSpec:
    inspection_id: str
    site_ref: str
    seeds: tuple[str, ...]
    scope: Scope
    budget: Budget = Budget(
        max_pages=120, max_candidates=1000, max_depth=4, max_duration_seconds=120.0
    )
    profile: SiteProfile | None = None
    known_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty("inspection_id", self.inspection_id)
        _require_non_empty("site_ref", self.site_ref)
        if not self.seeds and not (self.profile and self.profile.seeds):
            raise ValueError("inspection requires at least one seed")
        if self.profile and self.profile.site_ref != self.site_ref:
            raise ValueError("profile site_ref does not match inspection site_ref")


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    collection_id: str
    site_ref: str
    intent: CollectionIntent
    scope: Scope
    seeds: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    budget: Budget = Budget()
    profile: SiteProfile | None = None
    known_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty("collection_id", self.collection_id)
        _require_non_empty("site_ref", self.site_ref)
        if not isinstance(self.intent, CollectionIntent):
            try:
                object.__setattr__(self, "intent", CollectionIntent(self.intent))
            except ValueError as exc:
                raise ValueError(
                    f"unsupported collection intent: {self.intent}"
                ) from exc
        if self.profile and self.profile.site_ref != self.site_ref:
            raise ValueError("profile site_ref does not match collection site_ref")
        if self.intent is CollectionIntent.TARGETED_REFRESH and not self.targets:
            raise ValueError("targeted_refresh requires targets")
        if (
            self.intent is CollectionIntent.SITE_SWEEP
            and not self.seeds
            and not (self.profile and self.profile.seeds)
        ):
            raise ValueError("site_sweep requires at least one seed")


@dataclass(frozen=True, slots=True)
class PageCandidate:
    url: str
    canonical_url: str
    depth: int
    discovery_sources: tuple[str, ...] = ()
    page_type_hint: PageType = PageType.OTHER
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("candidate confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    ref: str
    kind: str
    sha256: str
    media_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class Attachment:
    url: str
    name: str = ""
    media_type: str = ""


@dataclass(frozen=True, slots=True)
class PageIssue:
    code: str
    url: str
    stage: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class Exclusion:
    url: str
    reason: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class RouteFamily:
    pattern: str
    sample_urls: tuple[str, ...]
    page_types: tuple[PageType, ...]
    count: int


@dataclass(frozen=True, slots=True)
class CoverageReport:
    stop_reason: StopReason
    discovery_sources_attempted: tuple[str, ...]
    discovery_sources_succeeded: tuple[str, ...]
    candidate_count: int
    visited_count: int
    page_count: int
    excluded_count: int
    duplicate_count: int
    failed_count: int
    frontier_converged: bool
    limits_reached: tuple[str, ...] = ()
    known_blind_spots: tuple[str, ...] = ()
    route_family_stats: tuple[RouteFamily, ...] = ()
    page_type_stats: tuple[tuple[PageType, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileDraft:
    site_ref: str
    suggested_seeds: tuple[str, ...]
    suggested_scope: Scope
    route_families: tuple[RouteFamily, ...]
    rendering_needed: bool
    evidence: tuple[str, ...]
    unknowns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WebsitePage:
    record_id: str
    collection_id: str
    site_ref: str
    url: str
    final_url: str
    canonical_url: str
    page_type: PageType
    classification_confidence: float
    classification_evidence: tuple[str, ...]
    title: str
    summary: str
    body: str
    section: str
    published_at: datetime | None
    published_at_confidence: float
    modified_at: datetime | None
    observed_at: datetime
    http_status: int
    media_type: str
    language: str
    fetch_outcome: str
    evidence_refs: tuple[EvidenceRef, ...]
    profile_ref: str | None
    discovery_sources: tuple[str, ...]
    outbound_sources: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    schema_type: str = field(default=SCHEMA_PAGE, init=False)

    def __post_init__(self) -> None:
        if not 0 <= self.classification_confidence <= 1:
            raise ValueError("classification confidence must be between 0 and 1")
        if not 0 <= self.published_at_confidence <= 1:
            raise ValueError("published_at confidence must be between 0 and 1")

    def to_payload(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True, slots=True)
class SiteInspection:
    inspection_id: str
    site_ref: str
    started_at: datetime
    finished_at: datetime
    candidates: tuple[PageCandidate, ...]
    route_families: tuple[RouteFamily, ...]
    page_type_samples: tuple[WebsitePage, ...]
    profile_draft: ProfileDraft
    coverage: CoverageReport
    issues: tuple[PageIssue, ...] = ()
    schema_type: str = field(default=SCHEMA_DISCOVERY, init=False)

    def to_payload(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True, slots=True)
class Usage:
    requests: int
    successful_pages: int
    failed_pages: int
    rendered_pages: int
    bytes_received: int


@dataclass(frozen=True, slots=True)
class CollectionResult:
    collection_id: str
    site_ref: str
    status: CollectionStatus
    started_at: datetime
    finished_at: datetime
    pages: tuple[WebsitePage, ...]
    exclusions: tuple[Exclusion, ...]
    issues: tuple[PageIssue, ...]
    coverage: CoverageReport
    profile_ref: str | None
    usage: Usage
    schema_type: str = field(default=SCHEMA_COLLECTION, init=False)

    def to_payload(self) -> dict[str, Any]:
        return _jsonable(self)


def stable_record_id(collection_id: str, canonical_url: str) -> str:
    digest = hashlib.sha256(f"{collection_id}\n{canonical_url}".encode()).hexdigest()
    return f"page-{digest[:32]}"


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _evidence_kind_for_ref(kind: str) -> str:
    """Return the URI-safe representation of an evidence kind."""

    if not isinstance(kind, str) or not kind.strip():
        return "evidence"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", kind).strip("-") or "evidence"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value
