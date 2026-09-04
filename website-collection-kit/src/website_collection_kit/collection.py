"""The deep website inspection and collection module."""

from __future__ import annotations

import asyncio
import gzip
import json
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

from .contracts import (
    Attachment,
    CollectionIntent,
    CollectionResult,
    CollectionSpec,
    CollectionStatus,
    CoverageReport,
    DiscoverySource,
    Exclusion,
    InspectionSpec,
    PageCandidate,
    PageIssue,
    PageType,
    ProfileDraft,
    RouteFamily,
    SiteInspection,
    SiteProfile,
    StopReason,
    Usage,
    WebsitePage,
    _utc_now,
    stable_record_id,
)
from .errors import (
    AcquisitionError,
    InvalidSpecError,
    NoAcquisitionCapabilityError,
    ProfileIncompatibleError,
    RequiredEvidenceUnavailableError,
)
from .evidence import MemoryEvidenceStore
from .interpretation import HtmlInterpreter
from .ports import AcquisitionPort, EvidencePayload, FetchRequest, FetchResponse
from .url_policy import UrlPolicy


_PRIVATE_ENDPOINT_MARKERS = (
    "login",
    "logout",
    "signin",
    "auth",
    "token",
    "captcha",
    "member",
    "account",
    "order",
    "pay",
    "payment",
    "password",
    "session",
)
_MUTATING_ENDPOINT_MARKERS = (
    "create",
    "delete",
    "destroy",
    "insert",
    "remove",
    "save",
    "submit",
    "update",
    "upload",
)
_COMMON_SITEMAP_PATHS = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap/sitemap.xml",
    "/sitemap.xml.gz",
    "/sitemap_index.xml.gz",
)


@dataclass(frozen=True, slots=True)
class _FrontierItem:
    url: str
    canonical_url: str
    depth: int
    sources: tuple[str, ...]
    priority: int = 1


class _Frontier:
    def __init__(self, policy: UrlPolicy, budget: int) -> None:
        self.policy = policy
        self.budget = budget
        self._pending: dict[int, deque[_FrontierItem]] = {}
        self._queued: dict[str, _FrontierItem] = {}
        self._known: dict[str, _FrontierItem] = {}
        self.duplicate_count = 0
        self.rejected_count = 0
        self.candidate_limit_hit = False

    def add(
        self,
        raw_url: str,
        *,
        base_url: str | None,
        depth: int,
        source: str,
        candidates: list[PageCandidate],
        exclusions: list[Exclusion],
    ) -> bool:
        decision = self.policy.decide(raw_url, base_url)
        if not decision.accepted:
            self.rejected_count += 1
            exclusions.append(
                Exclusion(
                    url=decision.canonical_url or raw_url,
                    reason=decision.reason,
                    source=source,
                )
            )
            return False
        if decision.canonical_url in self._known:
            previous = self._known[decision.canonical_url]
            sources = tuple(dict.fromkeys(previous.sources + (source,)))
            priority = min(previous.priority, _frontier_priority(source, raw_url))
            updated = _FrontierItem(
                previous.url,
                previous.canonical_url,
                min(previous.depth, depth),
                sources,
                priority,
            )
            self._known[decision.canonical_url] = updated
            if priority < previous.priority and decision.canonical_url in self._queued:
                # A lower-priority script hint may be seen before a normal
                # HTML link.  Requeue the same candidate at the better
                # priority; the old queue entry is ignored by identity in
                # ``pop``.  No candidate is removed by this optimization.
                self._queued[decision.canonical_url] = updated
                self._pending.setdefault(priority, deque()).append(updated)
            for index, candidate in enumerate(candidates):
                if candidate.canonical_url == decision.canonical_url:
                    candidates[index] = replace(
                        candidate,
                        depth=min(candidate.depth, depth),
                        discovery_sources=sources,
                    )
                    break
            self.duplicate_count += 1
            return False
        if len(self._known) >= self.budget:
            self.candidate_limit_hit = True
            exclusions.append(
                Exclusion(
                    url=decision.canonical_url,
                    reason="candidate_budget_exhausted",
                    source=source,
                )
            )
            return False
        item = _FrontierItem(
            url=decision.canonical_url,
            canonical_url=decision.canonical_url,
            depth=depth,
            sources=(source,),
            priority=_frontier_priority(source, raw_url),
        )
        self._known[decision.canonical_url] = item
        self._queued[decision.canonical_url] = item
        self._pending.setdefault(item.priority, deque()).append(item)
        candidates.append(
            PageCandidate(
                url=item.url,
                canonical_url=item.canonical_url,
                depth=depth,
                discovery_sources=(source,),
            )
        )
        return True

    def has_pending(self) -> bool:
        return bool(self._queued)

    def pop(self) -> _FrontierItem:
        for priority in sorted(self._pending):
            queue = self._pending[priority]
            while queue:
                item = queue.popleft()
                if self._queued.get(item.canonical_url) is not item:
                    continue
                self._queued.pop(item.canonical_url, None)
                if not queue:
                    del self._pending[priority]
                return self._known[item.canonical_url]
            del self._pending[priority]
        raise IndexError("frontier is empty")

    def update_sources(self, canonical_url: str, source: str) -> None:
        item = self._known.get(canonical_url)
        if item:
            self._known[canonical_url] = _FrontierItem(
                item.url,
                item.canonical_url,
                item.depth,
                tuple(dict.fromkeys(item.sources + (source,))),
                item.priority,
            )

    def item_for(self, canonical_url: str) -> _FrontierItem | None:
        return self._known.get(canonical_url)


class _BatchGate:
    """Release response processing in the order in which a batch was formed."""

    def __init__(self, size: int) -> None:
        self._turns = [asyncio.Event() for _ in range(size + 1)]
        self._turns[0].set()

    async def wait(self, index: int) -> None:
        await self._turns[index].wait()

    def done_callback(self, index: int):
        def release(_task) -> None:
            self._turns[index + 1].set()

        return release


class _RobotsRules:
    def __init__(
        self,
        disallow: Iterable[str] = (),
        allow: Iterable[str] = (),
        sitemaps: Iterable[str] = (),
    ) -> None:
        self.disallow = tuple(path for path in disallow if path)
        self.allow = tuple(path for path in allow if path)
        self.sitemaps = tuple(dict.fromkeys(sitemaps))

    def allows(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        query = urlsplit(url).query
        if query:
            path += "?" + query
        matches = [
            (len(rule), False)
            for rule in self.disallow
            if _robots_rule_matches(rule, path)
        ]
        matches.extend(
            (len(rule), True) for rule in self.allow if _robots_rule_matches(rule, path)
        )
        if not matches:
            return True
        _, allowed = max(matches, key=lambda value: (value[0], value[1]))
        return allowed


def _parse_robots(body: bytes) -> _RobotsRules:
    text = body.decode("utf-8", errors="replace")
    active = False
    saw_agent = False
    disallow: list[str] = []
    allow: list[str] = []
    sitemaps: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            active = value == "*"
            saw_agent = saw_agent or active
        elif key == "disallow" and active:
            disallow.append(value)
        elif key == "allow" and active:
            allow.append(value)
        elif key == "sitemap" and value:
            sitemaps.append(value)
    if not saw_agent:
        disallow, allow = [], []
    return _RobotsRules(disallow, allow, sitemaps)


def _robots_rule_matches(rule: str, path: str) -> bool:
    end_anchored = rule.endswith("$")
    expression = rule[:-1] if end_anchored else rule
    expression = re.escape(expression).replace(r"\*", ".*")
    suffix = "$" if end_anchored else ""
    return re.match(r"^" + expression + suffix, path) is not None


def _discovery_unsafe_reason(raw_url: str, base_url: str) -> str | None:
    """Exclude only routes that are unsafe to probe during link discovery.

    Discovery is recall-first: a route that merely looks like a template,
    placeholder, list, or unknown CMS page remains a candidate and is allowed
    to prove itself through the response.  The pre-fetch filter is reserved
    for routes that are clearly private or likely to cause a write side
    effect.  Explicit seeds and targeted refresh URLs are not passed through
    this filter.
    """

    try:
        parsed = urlsplit(urljoin(base_url, raw_url))
    except ValueError:
        return None
    raw_path = parsed.path
    raw_query = parsed.query
    path = raw_path.lower()
    query = raw_query.lower()
    if re.search(
        r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|jpg|jpeg|png|gif|svg|mp4|mp3|webm|wav|mov)$",
        path,
        re.IGNORECASE,
    ):
        return None
    route_tokens = _discovery_route_tokens(raw_path, raw_query)
    segments = tuple(
        re.sub(r"[^a-z0-9]", "", segment)
        for segment in unquote(path).split("/")
        if segment
    )
    login_markers = ("login", "logout", "signin", "auth", "captcha", "password")
    if _contains_discovery_marker(route_tokens, login_markers):
        return "login_route"

    endpoint_route = any(
        segment in {"api", "ajax", "json", "rest", "graphql"}
        for segment in segments
    ) or path.endswith(".json")
    if endpoint_route and _contains_discovery_marker(
        route_tokens, _PRIVATE_ENDPOINT_MARKERS, allow_embedded=True
    ):
        return "private_endpoint"
    if _contains_discovery_marker(route_tokens, _MUTATING_ENDPOINT_MARKERS):
        return "mutating_endpoint"
    return None


def _discovery_route_tokens(path: str, query: str) -> tuple[str, ...]:
    """Return exact route words without matching markers inside normal words."""

    value = unquote(f"{path} {query}")
    # Split common camel-case endpoint names such as ``GetPayStatus`` while
    # keeping ``author`` distinct from the sensitive marker ``auth``.
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    tokens = []
    for token in re.split(r"[/_.?&=:#\-\s]+", value):
        normalised = re.sub(r"[^a-z0-9]", "", token.lower())
        if normalised:
            tokens.append(normalised)
    return tuple(tokens)


def _contains_discovery_marker(
    tokens: tuple[str, ...], markers: Iterable[str], *, allow_embedded: bool = False
) -> bool:
    marker_set = {marker.lower() for marker in markers}
    for token in tokens:
        if token in marker_set:
            return True
        if not allow_embedded:
            continue
        for marker in marker_set:
            if marker == "auth" and token.startswith(("author", "authority")):
                # ``author`` and ``authority`` are ordinary words, not
                # authentication routes.
                continue
            if marker in token:
                return True
    return False


@dataclass
class _RunState:
    operation_id: str
    site_ref: str
    started_at: datetime
    candidates: list[PageCandidate]
    pages: list[WebsitePage]
    issues: list[PageIssue]
    exclusions: list[Exclusion]
    visited: set[str]
    page_keys: set[str]
    attempted_sources: list[str]
    succeeded_sources: list[str]
    limits_reached: set[str]
    blind_spots: set[str]
    robots: dict[str, _RobotsRules]
    redirect_hops: dict[str, int]
    auxiliary_seen: set[tuple[str, str]]
    requests: int = 0
    rendered_pages: int = 0
    bytes_received: int = 0
    stop_reason: StopReason = StopReason.CONVERGED
    cancelled: bool = False
    fatal: bool = False

    def attempt_source(self, source: str) -> None:
        if source not in self.attempted_sources:
            self.attempted_sources.append(source)

    def succeed_source(self, source: str) -> None:
        self.attempt_source(source)
        if source not in self.succeeded_sources:
            self.succeeded_sources.append(source)


class WebsiteCollectionKit:
    """Inspect and collect public websites through a small intention-based API."""

    def __init__(
        self,
        acquisition: AcquisitionPort,
        *,
        evidence=None,
        interpreter: HtmlInterpreter | None = None,
        max_redirects: int = 5,
        max_parallel_fetches: int = 4,
    ) -> None:
        if not hasattr(acquisition, "fetch"):
            raise TypeError("acquisition must implement fetch")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        if max_parallel_fetches < 1:
            raise ValueError("max_parallel_fetches must be positive")
        if evidence is None:
            evidence = MemoryEvidenceStore()
        if not hasattr(evidence, "save"):
            raise TypeError("evidence must implement save")
        self.acquisition = acquisition
        self.evidence = evidence
        self.interpreter = interpreter or HtmlInterpreter()
        self.max_redirects = max_redirects
        self.max_parallel_fetches = max_parallel_fetches

    async def inspect_site(self, spec: InspectionSpec) -> SiteInspection:
        self._validate_inspection(spec)
        scope, profile = self._resolve_profile(spec.scope, spec.site_ref, spec.profile)
        state, _, frontier = await self._run(
            operation_id=spec.inspection_id,
            site_ref=spec.site_ref,
            seeds=tuple(spec.seeds) + tuple(profile.seeds if profile else ()),
            known_urls=spec.known_urls,
            scope=scope,
            budget=spec.budget,
            profile=profile,
            targets_only=False,
        )
        finished = _utc_now()
        route_families = _route_families(state.pages or tuple(state.candidates))
        draft = ProfileDraft(
            site_ref=spec.site_ref,
            suggested_seeds=_unique(
                tuple(spec.seeds) + tuple(profile.seeds if profile else ())
            )[:10],
            suggested_scope=scope,
            route_families=route_families,
            rendering_needed=bool(profile and profile.rendering_required)
            or state.rendered_pages > 0,
            evidence=tuple(_profile_evidence(state, frontier)),
            unknowns=tuple(sorted(state.blind_spots)),
        )
        coverage = _coverage(state, frontier, route_families)
        return SiteInspection(
            inspection_id=spec.inspection_id,
            site_ref=spec.site_ref,
            started_at=state.started_at,
            finished_at=finished,
            candidates=tuple(
                sorted(state.candidates, key=lambda item: item.canonical_url)
            ),
            route_families=route_families,
            page_type_samples=tuple(
                sorted(state.pages, key=lambda item: item.canonical_url)[:20]
            ),
            profile_draft=draft,
            coverage=coverage,
            issues=tuple(state.issues),
        )

    async def collect_site(self, spec: CollectionSpec) -> CollectionResult:
        self._validate_collection(spec, expected=CollectionIntent.SITE_SWEEP)
        scope, profile = self._resolve_profile(spec.scope, spec.site_ref, spec.profile)
        state, _, frontier = await self._run(
            operation_id=spec.collection_id,
            site_ref=spec.site_ref,
            seeds=tuple(spec.seeds) + tuple(profile.seeds if profile else ()),
            known_urls=spec.known_urls,
            scope=scope,
            budget=spec.budget,
            profile=profile,
            targets_only=False,
        )
        return self._collection_result(state, frontier, profile)

    async def refresh_pages(self, spec: CollectionSpec) -> CollectionResult:
        self._validate_collection(spec, expected=CollectionIntent.TARGETED_REFRESH)
        scope, profile = self._resolve_profile(spec.scope, spec.site_ref, spec.profile)
        state, _, frontier = await self._run(
            operation_id=spec.collection_id,
            site_ref=spec.site_ref,
            seeds=(),
            known_urls=spec.targets,
            scope=scope,
            budget=spec.budget,
            profile=profile,
            targets_only=True,
        )
        return self._collection_result(state, frontier, profile)

    async def close(self) -> None:
        close = getattr(self.acquisition, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    def _validate_inspection(self, spec: InspectionSpec) -> None:
        if not isinstance(spec, InspectionSpec):
            raise InvalidSpecError("inspect_site requires InspectionSpec")

    def _validate_collection(
        self, spec: CollectionSpec, *, expected: CollectionIntent
    ) -> None:
        if not isinstance(spec, CollectionSpec):
            raise InvalidSpecError("collection operation requires CollectionSpec")
        if spec.intent is not expected:
            raise InvalidSpecError(f"operation requires intent {expected.value}")

    def _resolve_profile(
        self,
        scope,
        site_ref: str,
        profile: SiteProfile | None,
    ) -> tuple[object, SiteProfile | None]:
        if profile is None:
            return scope, None
        if profile.site_ref != site_ref:
            raise ProfileIncompatibleError("profile site_ref does not match operation")
        try:
            effective = scope.restrict_with(profile.scope) if profile.scope else scope
            if profile.excluded_path_patterns:
                effective = replace(
                    effective,
                    excluded_path_patterns=tuple(
                        dict.fromkeys(
                            effective.excluded_path_patterns
                            + profile.excluded_path_patterns
                        )
                    ),
                )
            return effective, profile
        except ValueError as exc:
            raise ProfileIncompatibleError(str(exc)) from exc

    async def _run(
        self,
        *,
        operation_id: str,
        site_ref: str,
        seeds: tuple[str, ...],
        known_urls: tuple[str, ...],
        scope,
        budget,
        profile: SiteProfile | None,
        targets_only: bool,
    ) -> tuple[_RunState, UrlPolicy, _Frontier]:
        state = _RunState(
            operation_id=operation_id,
            site_ref=site_ref,
            started_at=_utc_now(),
            candidates=[],
            pages=[],
            issues=[],
            exclusions=[],
            visited=set(),
            page_keys=set(),
            attempted_sources=[],
            succeeded_sources=[],
            limits_reached=set(),
            blind_spots=set(),
            robots={},
            redirect_hops={},
            auxiliary_seen=set(),
        )
        policy = UrlPolicy(scope)
        frontier = _Frontier(policy, budget.max_candidates)
        for seed in _unique(seeds):
            state.attempt_source(DiscoverySource.EXPLICIT_SEED.value)
            if frontier.add(
                seed,
                base_url=None,
                depth=0,
                source=DiscoverySource.EXPLICIT_SEED.value,
                candidates=state.candidates,
                exclusions=state.exclusions,
            ):
                state.succeed_source(DiscoverySource.EXPLICIT_SEED.value)
        for url in _unique(known_urls):
            state.attempt_source(DiscoverySource.KNOWN_URL.value)
            if frontier.add(
                url,
                base_url=None,
                depth=0,
                source=DiscoverySource.KNOWN_URL.value,
                candidates=state.candidates,
                exclusions=state.exclusions,
            ):
                state.succeed_source(DiscoverySource.KNOWN_URL.value)
        if not frontier.has_pending():
            if targets_only:
                raise InvalidSpecError("operation has no usable URL target")
            raise InvalidSpecError("operation has no usable URL seed")

        deadline = asyncio.get_running_loop().time() + budget.max_duration_seconds
        try:
            if not targets_only:
                await self._discover_auxiliary(
                    state,
                    frontier,
                    policy,
                    profile,
                    seeds,
                    deadline,
                    include_sitemaps=True,
                )
            else:
                await self._discover_auxiliary(
                    state,
                    frontier,
                    policy,
                    profile,
                    known_urls,
                    deadline,
                    include_sitemaps=False,
                )
            while frontier.has_pending():
                if asyncio.get_running_loop().time() >= deadline:
                    state.limits_reached.add("max_duration_seconds")
                    state.stop_reason = StopReason.BUDGET_EXHAUSTED
                    break
                if len(state.visited) >= budget.max_pages:
                    state.limits_reached.add("max_pages")
                    state.stop_reason = StopReason.BUDGET_EXHAUSTED
                    break
                batch: list[_FrontierItem] = []
                while (
                    frontier.has_pending()
                    and len(batch) < self.max_parallel_fetches
                    and len(state.visited) < budget.max_pages
                ):
                    item = frontier.pop()
                    if item.canonical_url in state.visited:
                        frontier.duplicate_count += 1
                        continue
                    state.visited.add(item.canonical_url)
                    if item.depth > budget.max_depth:
                        state.limits_reached.add("max_depth")
                        state.stop_reason = StopReason.BUDGET_EXHAUSTED
                        state.exclusions.append(
                            Exclusion(item.url, "max_depth", "frontier")
                        )
                        continue
                    if not await self._robots_allowed(
                        item.url, scope, policy, state, deadline
                    ):
                        state.exclusions.append(
                            Exclusion(item.url, "robots_disallowed", "robots")
                        )
                        continue
                    batch.append(item)
                if batch:
                    remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                    try:
                        gate = _BatchGate(len(batch))
                        tasks = []
                        for index, item in enumerate(batch):
                            task = asyncio.create_task(
                                self._visit_page(
                                    state,
                                    frontier,
                                    policy,
                                    profile,
                                    item,
                                    budget.max_depth,
                                    deadline,
                                    not targets_only,
                                    gate=gate,
                                    batch_index=index,
                                )
                            )
                            task.add_done_callback(gate.done_callback(index))
                            tasks.append(task)
                        try:
                            await asyncio.wait_for(
                                asyncio.gather(*tasks), timeout=remaining
                            )
                        except BaseException:
                            # ``gather`` propagates the first child exception but
                            # does not cancel its siblings.  Drain every child so
                            # a failed or cancelled operation cannot keep mutating
                            # the run state after its result has been returned.
                            for task in tasks:
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            raise
                    except TimeoutError:
                        state.limits_reached.add("max_duration_seconds")
                        state.stop_reason = StopReason.BUDGET_EXHAUSTED
                        break
            else:
                if (
                    frontier.candidate_limit_hit
                    or state.stop_reason is StopReason.BUDGET_EXHAUSTED
                ):
                    state.stop_reason = StopReason.BUDGET_EXHAUSTED
                elif state.stop_reason is StopReason.CONVERGED:
                    state.stop_reason = StopReason.CONVERGED
        except asyncio.CancelledError:
            state.cancelled = True
            state.stop_reason = StopReason.CANCELLED
        except RequiredEvidenceUnavailableError:
            state.fatal = True
            state.stop_reason = StopReason.FAILED
            raise
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            state.fatal = True
            state.stop_reason = StopReason.FAILED
            state.issues.append(
                PageIssue(
                    "collection_failed", "", "collection", str(exc), retryable=False
                )
            )
        return state, policy, frontier

    async def _discover_auxiliary(
        self,
        state: _RunState,
        frontier: _Frontier,
        policy: UrlPolicy,
        profile: SiteProfile | None,
        seeds: tuple[str, ...],
        deadline: float,
        *,
        include_sitemaps: bool,
    ) -> None:
        origins = _origins(seeds)
        for origin in origins:
            if asyncio.get_running_loop().time() >= deadline:
                state.limits_reached.add("max_duration_seconds")
                state.stop_reason = StopReason.BUDGET_EXHAUSTED
                return
            rules = await self._load_robots(state, policy, origin, deadline)
            if include_sitemaps:
                configured = tuple(rules.sitemaps)
                conventional = tuple(
                    origin.rstrip("/") + path for path in _COMMON_SITEMAP_PATHS
                )
                sitemaps = tuple(dict.fromkeys(configured + conventional))
                for sitemap in sitemaps:
                    await self._read_sitemap(
                        state,
                        frontier,
                        policy,
                        sitemap,
                        deadline,
                        depth=0,
                        optional=not rules.sitemaps,
                    )
        if not include_sitemaps:
            return
        hints = tuple(profile.discovery_hints if profile else ())
        for hint in hints:
            state.attempt_source(DiscoverySource.PROFILE_HINT.value)
            lower = hint.lower()
            source = (
                DiscoverySource.RSS.value
                if any(token in lower for token in ("rss", "atom", "feed"))
                else DiscoverySource.SITEMAP.value
            )
            hint_urls = (hint,)
            if not urlsplit(hint).scheme and origins:
                hint_urls = tuple(
                    policy.resolve(hint, origin + "/") for origin in origins
                )
            for hint_url in hint_urls:
                if source == DiscoverySource.RSS.value:
                    await self._read_feed(state, frontier, policy, hint_url, deadline)
                else:
                    await self._read_sitemap(
                        state, frontier, policy, hint_url, deadline, depth=0
                    )

    async def _read_sitemap(
        self,
        state: _RunState,
        frontier: _Frontier,
        policy: UrlPolicy,
        url: str,
        deadline: float,
        *,
        depth: int,
        optional: bool = False,
    ) -> None:
        if depth > 3:
            state.blind_spots.add("sitemap_depth_or_time_limit")
            return
        if asyncio.get_running_loop().time() >= deadline:
            state.blind_spots.add("sitemap_depth_or_time_limit")
            state.limits_reached.add("max_duration_seconds")
            state.stop_reason = StopReason.BUDGET_EXHAUSTED
            return
        decision = policy.decide_auxiliary(url)
        if not decision.accepted:
            state.exclusions.append(
                Exclusion(url, decision.reason, DiscoverySource.SITEMAP.value)
            )
            return
        key = (DiscoverySource.SITEMAP.value, decision.canonical_url)
        if key in state.auxiliary_seen:
            return
        state.auxiliary_seen.add(key)
        state.attempt_source(DiscoverySource.SITEMAP.value)
        response = await self._fetch_auxiliary(
            state, url, policy, purpose="sitemap", deadline=deadline
        )
        if not response or response.status < 200 or response.status >= 300:
            state.blind_spots.add("sitemap_unavailable")
            return
        try:
            root = ElementTree.fromstring(_decompress_sitemap(response.body))
        except (ElementTree.ParseError, ValueError):
            if optional:
                state.blind_spots.add("sitemap_unavailable")
            else:
                state.issues.append(
                    PageIssue(
                        "invalid_sitemap", url, "discovery", "sitemap is not valid XML"
                    )
                )
            return
        name = _local_name(root.tag)
        if name not in {"sitemapindex", "urlset"}:
            if optional:
                state.blind_spots.add("sitemap_unavailable")
            else:
                state.issues.append(
                    PageIssue(
                        "invalid_sitemap", url, "discovery", "sitemap is not valid XML"
                    )
                )
            return
        state.succeed_source(DiscoverySource.SITEMAP.value)
        locations = [
            element.text.strip()
            for element in root.iter()
            if _local_name(element.tag) == "loc"
            and element.text
            and element.text.strip()
        ]
        if name == "sitemapindex":
            for child in locations:
                if frontier.candidate_limit_hit:
                    break
                await self._read_sitemap(
                    state, frontier, policy, child, deadline, depth=depth + 1
                )
        else:
            for candidate in locations:
                if frontier.candidate_limit_hit:
                    break
                frontier.add(
                    candidate,
                    base_url=response.final_url or url,
                    depth=0,
                    source=DiscoverySource.SITEMAP.value,
                    candidates=state.candidates,
                    exclusions=state.exclusions,
                )

    async def _read_feed(
        self,
        state: _RunState,
        frontier: _Frontier,
        policy: UrlPolicy,
        url: str,
        deadline: float,
    ) -> None:
        decision = policy.decide_auxiliary(url)
        if not decision.accepted:
            state.exclusions.append(
                Exclusion(url, decision.reason, DiscoverySource.RSS.value)
            )
            return
        key = (DiscoverySource.RSS.value, decision.canonical_url)
        if key in state.auxiliary_seen:
            return
        state.auxiliary_seen.add(key)
        state.attempt_source(DiscoverySource.RSS.value)
        response = await self._fetch_auxiliary(
            state, url, policy, purpose="feed", deadline=deadline
        )
        if not response or response.status < 200 or response.status >= 300:
            state.blind_spots.add("feed_unavailable")
            return
        try:
            root = ElementTree.fromstring(response.body)
        except (ElementTree.ParseError, ValueError):
            state.issues.append(
                PageIssue("invalid_feed", url, "discovery", "feed is not valid XML")
            )
            return
        if _local_name(root.tag) not in {"rss", "feed", "rdf"}:
            state.issues.append(
                PageIssue("invalid_feed", url, "discovery", "feed is not valid XML")
            )
            return
        state.succeed_source(DiscoverySource.RSS.value)
        for element in root.iter():
            if _local_name(element.tag) != "link":
                continue
            value = (element.text or "").strip() or element.attrib.get(
                "href", ""
            ).strip()
            if value:
                frontier.add(
                    value,
                    base_url=response.final_url or url,
                    depth=0,
                    source=DiscoverySource.RSS.value,
                    candidates=state.candidates,
                    exclusions=state.exclusions,
                )

    async def _fetch_auxiliary(
        self,
        state: _RunState,
        url: str,
        policy: UrlPolicy,
        *,
        purpose: str,
        deadline: float,
    ) -> FetchResponse | None:
        decision = policy.decide_auxiliary(url)
        if not decision.accepted:
            state.exclusions.append(Exclusion(url, decision.reason, purpose))
            return None
        current = decision.canonical_url
        for _ in range(self.max_redirects + 1):
            if asyncio.get_running_loop().time() >= deadline:
                state.limits_reached.add("max_duration_seconds")
                state.stop_reason = StopReason.BUDGET_EXHAUSTED
                return None
            try:
                response = await self._fetch_with_retry(
                    state,
                    FetchRequest(current, purpose=purpose, scope=policy.scope),
                    deadline=deadline,
                )
            except AcquisitionError as exc:
                state.issues.append(
                    PageIssue(exc.code, current, purpose, str(exc), exc.retryable)
                )
                return None
            if response.redirect_to:
                next_decision = policy.decide_auxiliary(
                    response.redirect_to, response.final_url or current
                )
                if not next_decision.accepted:
                    state.exclusions.append(
                        Exclusion(
                            next_decision.canonical_url or response.redirect_to,
                            "redirect_out_of_scope",
                            purpose,
                        )
                    )
                    return None
                current = next_decision.canonical_url
                continue
            final_decision = policy.decide_auxiliary(response.final_url or current)
            if not final_decision.accepted:
                state.exclusions.append(
                    Exclusion(response.final_url, "redirect_out_of_scope", purpose)
                )
                return None
            return response
        state.issues.append(
            PageIssue(
                "too_many_redirects",
                url,
                purpose,
                "auxiliary resource exceeded redirect limit",
            )
        )
        return None

    async def _load_robots(
        self,
        state: _RunState,
        policy: UrlPolicy,
        origin: str,
        deadline: float,
    ) -> _RobotsRules:
        key = _origin_key(origin)
        if not key:
            return _RobotsRules()
        cached = state.robots.get(key)
        if cached is not None:
            return cached
        if asyncio.get_running_loop().time() >= deadline:
            state.limits_reached.add("max_duration_seconds")
            state.stop_reason = StopReason.BUDGET_EXHAUSTED
            rules = _RobotsRules()
            state.robots[key] = rules
            return rules

        robots_url = origin.rstrip("/") + "/robots.txt"
        state.attempt_source(DiscoverySource.ROBOTS.value)
        response = await self._fetch_auxiliary(
            state, robots_url, policy, purpose="robots", deadline=deadline
        )
        rules = _RobotsRules()
        if response and response.status == 200:
            rules = _parse_robots(response.body)
            state.succeed_source(DiscoverySource.ROBOTS.value)
        else:
            state.blind_spots.add("robots_unavailable")
        state.robots[key] = rules
        return rules

    async def _visit_page(
        self,
        state: _RunState,
        frontier: _Frontier,
        policy: UrlPolicy,
        profile: SiteProfile | None,
        item: _FrontierItem,
        max_depth: int,
        deadline: float,
        follow_links: bool,
        *,
        gate: _BatchGate | None = None,
        batch_index: int = 0,
    ) -> None:
        rendering = _rendering_for(profile)
        try:
            response = await self._fetch_page(
                state, item.url, rendering, deadline, policy.scope
            )
        except AcquisitionError as exc:
            if gate is not None:
                await gate.wait(batch_index)
            state.issues.append(
                PageIssue(exc.code, item.url, "fetch", str(exc), exc.retryable)
            )
            if exc.code in {"rendering_required", "browser_fetch_failed"}:
                state.blind_spots.add("rendered_content_unavailable")
            return
        if gate is not None:
            await gate.wait(batch_index)
        if response is None:
            return
        if response.rendering_fallback_failed:
            state.blind_spots.add("rendered_content_unavailable")
        if response.redirect_to:
            state.attempt_source(DiscoverySource.REDIRECT.value)
            redirect_hops = state.redirect_hops.get(item.canonical_url, 0) + 1
            if redirect_hops > self.max_redirects:
                state.issues.append(
                    PageIssue(
                        "too_many_redirects",
                        item.url,
                        "fetch",
                        "page exceeded redirect limit",
                    )
                )
                return
            decision = policy.decide(
                response.redirect_to, response.final_url or item.url
            )
            if decision.accepted:
                state.succeed_source(DiscoverySource.REDIRECT.value)
                state.redirect_hops[decision.canonical_url] = redirect_hops
                frontier.add(
                    decision.canonical_url,
                    base_url=None,
                    depth=item.depth,
                    source=DiscoverySource.REDIRECT.value,
                    candidates=state.candidates,
                    exclusions=state.exclusions,
                )
            else:
                state.exclusions.append(
                    Exclusion(
                        decision.canonical_url or response.redirect_to,
                        "redirect_out_of_scope",
                        DiscoverySource.REDIRECT.value,
                    )
                )
            return
        final_decision = policy.decide(response.final_url or item.url)
        if not final_decision.accepted:
            state.exclusions.append(
                Exclusion(response.final_url, "redirect_out_of_scope", "fetch")
            )
            return
        if response.status < 200 or response.status >= 300:
            state.issues.append(
                PageIssue(
                    "http_status",
                    item.url,
                    "fetch",
                    f"HTTP status {response.status}",
                    retryable=response.status >= 500,
                )
            )
            return
        if "playwright" in response.fetch_method:
            state.rendered_pages += 1
        state.bytes_received += len(response.body)
        evidence_refs = ()
        if response.body:
            try:
                evidence_refs = (
                    self.evidence.save(
                        EvidencePayload(
                            collection_id=state.operation_id,
                            url=response.final_url,
                            kind="html" if _is_html(response) else "resource",
                            content=response.body,
                            media_type=response.media_type
                            or "application/octet-stream",
                            headers=response.headers,
                        )
                    ),
                )
            except Exception as exc:
                raise RequiredEvidenceUnavailableError(
                    f"could not save evidence for {item.url}: {exc}"
                ) from exc
        if not _is_html(response):
            if _is_json(response):
                state.attempt_source(DiscoverySource.DATA_URL.value)
                values = _json_urls(response.body)
                if values:
                    state.succeed_source(DiscoverySource.DATA_URL.value)
                for value in values:
                    decision = policy.decide(value, response.final_url)
                    if decision.accepted:
                        frontier.add(
                            decision.canonical_url,
                            base_url=None,
                            depth=item.depth + 1,
                            source=DiscoverySource.DATA_URL.value,
                            candidates=state.candidates,
                            exclusions=state.exclusions,
                        )
            page = _resource_page(
                state,
                item,
                response,
                final_decision.canonical_url,
                evidence_refs,
                profile,
            )
            _update_candidate(
                state.candidates,
                item.canonical_url,
                page.page_type,
                page.classification_confidence,
            )
            state.pages.append(page)
            state.page_keys.add(page.canonical_url)
            return
        try:
            interpreted = self.interpreter.interpret(
                response, profile=profile, policy=policy
            )
        except Exception as exc:  # noqa: BLE001 - a custom interpreter must fail only this page
            state.issues.append(
                PageIssue(
                    "interpretation_failed",
                    item.url,
                    "interpretation",
                    str(exc),
                    retryable=False,
                )
            )
            page = _unclassified_page(
                state,
                item,
                response,
                final_decision.canonical_url,
                evidence_refs,
                profile,
            )
            _update_candidate(
                state.candidates,
                item.canonical_url,
                page.page_type,
                page.classification_confidence,
            )
            state.pages.append(page)
            state.page_keys.add(page.canonical_url)
            return
        if _looks_like_soft_not_found(interpreted.title, interpreted.body):
            state.issues.append(
                PageIssue(
                    "soft_not_found",
                    item.url,
                    "fetch",
                    "the server returned a not-found page with a successful HTTP status",
                    retryable=False,
                )
            )
        link_source = (
            DiscoverySource.DOM_LINK.value
            if "playwright" in response.fetch_method
            else DiscoverySource.HTML_LINK.value
        )
        state.attempt_source(link_source)
        for link in sorted(interpreted.links, key=_link_discovery_priority):
            if link.source == link_source:
                state.succeed_source(link_source)
            elif link.source in {
                DiscoverySource.DATA_URL.value,
                DiscoverySource.SCRIPT_URL.value,
                DiscoverySource.EMBEDDED_URL.value,
                DiscoverySource.META_REFRESH.value,
                DiscoverySource.ONCLICK.value,
            }:
                state.succeed_source(link.source)
        canonical = final_decision.canonical_url
        if interpreted.canonical_url:
            canonical_decision = policy.decide(
                interpreted.canonical_url, response.final_url
            )
            if canonical_decision.accepted:
                canonical = canonical_decision.canonical_url
            else:
                state.issues.append(
                    PageIssue(
                        "canonical_out_of_scope",
                        item.url,
                        "interpretation",
                        "canonical link points outside the operation scope",
                    )
                )
        if canonical in state.page_keys:
            frontier.duplicate_count += 1
            return
        attachments = tuple(
            _normalise_attachments(interpreted.attachments, response.final_url, policy)
        )
        outbound: list[str] = []
        for link in interpreted.links:
            decision = policy.decide(link.raw_url, response.final_url)
            if link.source == DiscoverySource.SITEMAP.value and follow_links:
                await self._read_sitemap(
                    state,
                    frontier,
                    policy,
                    policy.resolve(link.raw_url, response.final_url),
                    deadline,
                    depth=0,
                )
                continue
            if link.source == DiscoverySource.RSS.value and follow_links:
                await self._read_feed(
                    state,
                    frontier,
                    policy,
                    policy.resolve(link.raw_url, response.final_url),
                    deadline,
                )
                continue
            if link.source in {
                DiscoverySource.SITEMAP.value,
                DiscoverySource.RSS.value,
            }:
                continue
            if decision.accepted and follow_links:
                non_page_reason = _discovery_unsafe_reason(
                    link.raw_url, response.final_url
                )
                if non_page_reason:
                    state.exclusions.append(
                        Exclusion(
                            decision.canonical_url,
                            non_page_reason,
                            link.source,
                        )
                    )
                    continue
                frontier.add(
                    decision.canonical_url,
                    base_url=None,
                    depth=item.depth + 1,
                    source=link.source,
                    candidates=state.candidates,
                    exclusions=state.exclusions,
                )
            elif decision.accepted:
                # A targeted refresh records the page's links as evidence but
                # deliberately does not widen the requested target set.
                continue
            elif decision.reason == "external_host":
                outbound.append(policy.resolve(link.raw_url, response.final_url))
            else:
                # Keep exclusions explainable, but do not treat ordinary
                # search/media links as page failures.
                if decision.reason not in {"unsupported_scheme_or_missing_host"}:
                    state.exclusions.append(
                        Exclusion(
                            decision.canonical_url or link.raw_url,
                            decision.reason,
                            link.source,
                        )
                    )
        page = WebsitePage(
            record_id=stable_record_id(state.operation_id, canonical),
            collection_id=state.operation_id,
            site_ref=state.site_ref,
            url=item.url,
            final_url=response.final_url,
            canonical_url=canonical,
            page_type=interpreted.page_type,
            classification_confidence=interpreted.classification_confidence,
            classification_evidence=interpreted.classification_evidence,
            title=interpreted.title,
            summary=interpreted.summary,
            body=interpreted.body,
            section=interpreted.section,
            published_at=interpreted.published_at,
            published_at_confidence=interpreted.published_at_confidence,
            modified_at=interpreted.modified_at,
            observed_at=_utc_now(),
            http_status=response.status,
            media_type=response.media_type or "text/html",
            language=interpreted.language,
            fetch_outcome="success",
            evidence_refs=evidence_refs,
            profile_ref=f"{profile.profile_id}@{profile.version}" if profile else None,
            discovery_sources=item.sources,
            outbound_sources=tuple(dict.fromkeys(outbound)),
            attachments=attachments,
        )
        _update_candidate(
            state.candidates,
            item.canonical_url,
            page.page_type,
            page.classification_confidence,
        )
        state.pages.append(page)
        state.page_keys.add(canonical)

    async def _fetch_page(
        self, state: _RunState, url: str, rendering, deadline: float, scope
    ) -> FetchResponse | None:
        if asyncio.get_running_loop().time() >= deadline:
            state.limits_reached.add("max_duration_seconds")
            state.stop_reason = StopReason.BUDGET_EXHAUSTED
            return None
        return await self._fetch_with_retry(
            state,
            FetchRequest(url, rendering=rendering, purpose="page", scope=scope),
            deadline=deadline,
        )

    async def _fetch_with_retry(
        self,
        state: _RunState,
        request: FetchRequest,
        *,
        deadline: float | None = None,
    ) -> FetchResponse:
        last: AcquisitionError | None = None
        for attempt in range(2):
            remaining = (
                None
                if deadline is None
                else deadline - asyncio.get_running_loop().time()
            )
            if remaining is not None and remaining <= 0:
                state.limits_reached.add("max_duration_seconds")
                state.stop_reason = StopReason.BUDGET_EXHAUSTED
                raise AcquisitionError(
                    "operation_deadline_exceeded", "collection deadline exceeded"
                )
            state.requests += 1
            try:
                if remaining is None:
                    response = await self.acquisition.fetch(request)
                else:
                    try:
                        response = await asyncio.wait_for(
                            self.acquisition.fetch(request), timeout=remaining
                        )
                    except TimeoutError as exc:
                        state.limits_reached.add("max_duration_seconds")
                        state.stop_reason = StopReason.BUDGET_EXHAUSTED
                        raise AcquisitionError(
                            "operation_deadline_exceeded",
                            "collection deadline exceeded",
                        ) from exc
                if (
                    deadline is not None
                    and asyncio.get_running_loop().time() >= deadline
                ):
                    state.limits_reached.add("max_duration_seconds")
                    state.stop_reason = StopReason.BUDGET_EXHAUSTED
                    raise AcquisitionError(
                        "operation_deadline_exceeded", "collection deadline exceeded"
                    )
                return response
            except AcquisitionError as exc:
                last = exc
                if not exc.retryable or attempt == 1:
                    raise
                delay = 0.1 * (attempt + 1)
                if deadline is not None:
                    delay = min(
                        delay, max(0.0, deadline - asyncio.get_running_loop().time())
                    )
                await asyncio.sleep(delay)
            except NoAcquisitionCapabilityError:
                raise
            except Exception as exc:
                raise AcquisitionError(
                    "acquisition_failed", str(exc), retryable=False
                ) from exc
        assert last is not None
        raise last

    async def _robots_allowed(
        self,
        url: str,
        scope,
        policy: UrlPolicy,
        state: _RunState,
        deadline: float,
    ) -> bool:
        if not scope.respect_robots:
            return True
        rules = await self._load_robots(state, policy, _origin_key(url), deadline)
        return rules.allows(url)

    def _collection_result(
        self, state: _RunState, frontier: _Frontier, profile: SiteProfile | None
    ) -> CollectionResult:
        route_families = _route_families(state.pages)
        coverage = _coverage(state, frontier, route_families)
        failed_count = sum(
            1
            for issue in state.issues
            if issue.stage in {"fetch", "interpretation", "discovery"}
        )
        if state.cancelled:
            status = CollectionStatus.CANCELLED
        elif state.fatal or (not state.pages and failed_count > 0):
            status = CollectionStatus.FAILED
        elif state.stop_reason is not StopReason.CONVERGED or failed_count:
            status = CollectionStatus.PARTIAL
        else:
            status = CollectionStatus.COMPLETED
        return CollectionResult(
            collection_id=state.operation_id,
            site_ref=state.site_ref,
            status=status,
            started_at=state.started_at,
            finished_at=_utc_now(),
            pages=tuple(sorted(state.pages, key=lambda item: item.canonical_url)),
            exclusions=tuple(state.exclusions),
            issues=tuple(state.issues),
            coverage=coverage,
            profile_ref=f"{profile.profile_id}@{profile.version}" if profile else None,
            usage=Usage(
                state.requests,
                len(state.pages),
                failed_count,
                state.rendered_pages,
                state.bytes_received,
            ),
        )


def _rendering_for(profile: SiteProfile | None):
    from .contracts import RenderingRequirement

    if profile and profile.rendering_required:
        return RenderingRequirement.REQUIRED
    if profile and profile.rendering_fallback:
        return RenderingRequirement.PREFERRED
    return RenderingRequirement.STATIC


def _is_html(response: FetchResponse) -> bool:
    media_type = (response.media_type or "").lower()
    if media_type.startswith(("text/html", "application/xhtml")):
        return True
    sample = response.body[:512].lstrip().lower()
    return sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _is_json(response: FetchResponse) -> bool:
    media_type = (response.media_type or "").lower()
    if "json" in media_type:
        return True
    return response.body[:80].lstrip().startswith((b"{", b"["))


def _decompress_sitemap(body: bytes) -> bytes:
    """Accept the gzip form commonly used for large sitemap files."""

    if body.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body


def _link_discovery_priority(link) -> tuple[int, int]:
    """Visit likely content links before script/template and binary resources."""

    source = getattr(link, "source", "")
    raw_url = getattr(link, "raw_url", "")
    if source in {DiscoverySource.SITEMAP.value, DiscoverySource.RSS.value}:
        return (0, 0)
    if source == DiscoverySource.META_REFRESH.value:
        return (1, 0)
    if source in {
        DiscoverySource.HTML_LINK.value,
        DiscoverySource.DOM_LINK.value,
        DiscoverySource.DATA_URL.value,
        DiscoverySource.ONCLICK.value,
    }:
        return (2 if not _looks_like_resource_url(raw_url) else 4, 0)
    if source == DiscoverySource.SCRIPT_URL.value:
        return (3, 0)
    if source == DiscoverySource.EMBEDDED_URL.value:
        return (4, 0)
    return (3, 0)


def _frontier_priority(source: str, raw_url: str) -> int:
    """Order candidates without deleting low-confidence discovery evidence."""

    if source in {
        DiscoverySource.EXPLICIT_SEED.value,
        DiscoverySource.KNOWN_URL.value,
        DiscoverySource.SITEMAP.value,
        DiscoverySource.RSS.value,
    }:
        return 0
    if source in {
        DiscoverySource.HTML_LINK.value,
        DiscoverySource.DOM_LINK.value,
        DiscoverySource.ONCLICK.value,
        DiscoverySource.META_REFRESH.value,
    }:
        return 4 if _looks_like_resource_url(raw_url) else 1
    if source == DiscoverySource.DATA_URL.value:
        return 2
    if source == DiscoverySource.SCRIPT_URL.value:
        return 3
    if source == DiscoverySource.EMBEDDED_URL.value:
        return 4
    return 2


def _looks_like_resource_url(raw_url: str) -> bool:
    path = urlsplit(raw_url).path.lower()
    return bool(
        re.search(
            r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|jpe?g|png|gif|svg|webp|mp4|mp3|webm|wav|mov)$",
            path,
            re.IGNORECASE,
        )
        or "/api/attach/" in path
        or "/api/file/" in path
        or "/download" in path
    )


def _looks_like_soft_not_found(title: str, body: str) -> bool:
    """Reject obvious CMS 404 pages that incorrectly return HTTP 200."""

    title_value = re.sub(r"\s+", " ", title.strip().lower())
    body_value = re.sub(r"\s+", " ", body[:1_200].strip().lower())
    if re.search(r"(?:^|\s|[-|])404(?:\s|$|[-|])", title_value):
        return True
    if any(
        marker in title_value
        for marker in ("page not found", "页面未找到", "页面不存在", "找不到页面")
    ):
        return True
    if len(body_value) <= 300 and any(
        marker in body_value
        for marker in ("page not found", "404 -", "404页面", "页面未找到", "页面不存在")
    ):
        return True
    return False


def _json_urls(body: bytes) -> tuple[str, ...]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return ()
    result: list[str] = []
    keys = {"url", "href", "link", "uri", "permalink", "next", "target"}

    pending = deque([value])
    while pending:
        node = pending.popleft()
        if isinstance(node, dict):
            for key, child in node.items():
                if (
                    isinstance(key, str)
                    and isinstance(child, str)
                    and key.lower() in keys
                ):
                    result.append(child)
                elif isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(node, list):
            pending.extend(node)
    return tuple(dict.fromkeys(result))


def _resource_page(
    state: _RunState,
    item: _FrontierItem,
    response: FetchResponse,
    canonical: str,
    evidence_refs,
    profile: SiteProfile | None = None,
) -> WebsitePage:
    filename = unquote(urlsplit(canonical).path.rsplit("/", 1)[-1]) or "resource"
    return WebsitePage(
        record_id=stable_record_id(state.operation_id, canonical),
        collection_id=state.operation_id,
        site_ref=state.site_ref,
        url=item.url,
        final_url=response.final_url,
        canonical_url=canonical,
        page_type=PageType.ATTACHMENT,
        classification_confidence=0.98,
        classification_evidence=(f"media_type:{response.media_type or 'unknown'}",),
        title=filename,
        summary="",
        body="",
        section="",
        published_at=None,
        published_at_confidence=0.0,
        modified_at=None,
        observed_at=_utc_now(),
        http_status=response.status,
        media_type=response.media_type or "application/octet-stream",
        language="",
        fetch_outcome="success",
        evidence_refs=evidence_refs,
        profile_ref=f"{profile.profile_id}@{profile.version}" if profile else None,
        discovery_sources=item.sources,
    )


def _unclassified_page(
    state: _RunState,
    item: _FrontierItem,
    response: FetchResponse,
    canonical: str,
    evidence_refs,
    profile: SiteProfile | None,
) -> WebsitePage:
    """Keep a successfully fetched page when semantic interpretation fails."""

    return WebsitePage(
        record_id=stable_record_id(state.operation_id, canonical),
        collection_id=state.operation_id,
        site_ref=state.site_ref,
        url=item.url,
        final_url=response.final_url,
        canonical_url=canonical,
        page_type=PageType.OTHER,
        classification_confidence=0.0,
        classification_evidence=("interpretation_failed",),
        title="",
        summary="",
        body=_decode_unclassified_body(response),
        section="",
        published_at=None,
        published_at_confidence=0.0,
        modified_at=None,
        observed_at=_utc_now(),
        http_status=response.status,
        media_type=response.media_type or "text/html",
        language="",
        fetch_outcome="success",
        evidence_refs=evidence_refs,
        profile_ref=f"{profile.profile_id}@{profile.version}" if profile else None,
        discovery_sources=item.sources,
    )


def _decode_unclassified_body(response: FetchResponse) -> str:
    """Expose a readable fallback body without making interpretation mandatory."""

    content_type = " ".join(
        f"{key}:{value}" for key, value in response.headers.items()
    )
    match = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.IGNORECASE)
    encodings = [match.group(1)] if match else []
    encodings.extend(("utf-8", "gb18030"))
    for encoding in dict.fromkeys(encodings):
        try:
            return response.body.decode(encoding).replace("\x00", "")
        except (LookupError, UnicodeDecodeError):
            continue
    return response.body.decode("utf-8", errors="replace").replace("\x00", "")


def _normalise_attachments(
    attachments: Iterable[Attachment], base_url: str, policy: UrlPolicy
) -> Iterable[Attachment]:
    for attachment in attachments:
        decision = policy.decide(attachment.url, base_url)
        if decision.accepted:
            yield Attachment(
                url=decision.canonical_url,
                name=attachment.name,
                media_type=attachment.media_type,
            )


def _coverage(
    state: _RunState, frontier: _Frontier, route_families: tuple[RouteFamily, ...]
) -> CoverageReport:
    counts = Counter(page.page_type for page in state.pages)
    limits = set(state.limits_reached)
    if frontier.candidate_limit_hit:
        limits.add("max_candidates")
    return CoverageReport(
        stop_reason=state.stop_reason,
        discovery_sources_attempted=tuple(state.attempted_sources),
        discovery_sources_succeeded=tuple(state.succeeded_sources),
        candidate_count=len(state.candidates),
        visited_count=len(state.visited),
        page_count=len(state.pages),
        excluded_count=len(state.exclusions),
        duplicate_count=frontier.duplicate_count,
        failed_count=sum(
            1
            for issue in state.issues
            if issue.stage in {"fetch", "interpretation", "discovery"}
        ),
        frontier_converged=state.stop_reason is StopReason.CONVERGED
        and not frontier.has_pending(),
        limits_reached=tuple(sorted(limits)),
        known_blind_spots=tuple(sorted(state.blind_spots)),
        route_family_stats=route_families,
        page_type_stats=tuple(sorted(counts.items(), key=lambda item: item[0].value)),
    )


def _route_families(items: Iterable[object]) -> tuple[RouteFamily, ...]:
    groups: dict[str, list[object]] = {}
    for item in items:
        url = getattr(item, "canonical_url", "")
        path = urlsplit(url).path or "/"
        parts = [part for part in path.split("/") if part]
        pattern = "/" + parts[0] + "/*" if parts else "/"
        groups.setdefault(pattern, []).append(item)
    result = []
    for pattern, values in sorted(groups.items()):
        urls = tuple(
            dict.fromkeys(
                getattr(value, "canonical_url", "")
                for value in values
                if getattr(value, "canonical_url", "")
            )
        )[:5]
        page_types = tuple(
            dict.fromkeys(
                getattr(value, "page_type", PageType.OTHER) for value in values
            )
        )
        result.append(
            RouteFamily(
                pattern=pattern,
                sample_urls=urls,
                page_types=page_types,
                count=len(values),
            )
        )
    return tuple(result)


def _profile_evidence(state: _RunState, frontier: _Frontier) -> list[str]:
    evidence = [
        f"candidates:{len(state.candidates)}",
        f"visited:{len(state.visited)}",
        f"pages:{len(state.pages)}",
    ]
    if state.succeeded_sources:
        evidence.append("sources:" + ",".join(state.succeeded_sources))
    if frontier.duplicate_count:
        evidence.append(f"duplicates:{frontier.duplicate_count}")
    return evidence


def _origins(seeds: Iterable[str]) -> tuple[str, ...]:
    result = []
    for seed in seeds:
        if not isinstance(seed, str):
            continue
        try:
            parsed = urlsplit(seed)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            continue
        if parsed.scheme.lower() in {"http", "https"} and host:
            try:
                host = host.lower().rstrip(".").encode("idna").decode("ascii")
            except UnicodeError:
                continue
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = host
            if port is not None and not (
                (parsed.scheme.lower() == "http" and port == 80)
                or (parsed.scheme.lower() == "https" and port == 443)
            ):
                netloc = f"{host}:{port}"
            origin = f"{parsed.scheme.lower()}://{netloc}"
            if origin not in result:
                result.append(origin)
    return tuple(result)


def _origin_key(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        )
    )


def _update_candidate(
    candidates: list[PageCandidate],
    canonical_url: str,
    page_type: PageType,
    confidence: float,
) -> None:
    for index, candidate in enumerate(candidates):
        if candidate.canonical_url == canonical_url:
            candidates[index] = replace(
                candidate, page_type_hint=page_type, confidence=confidence
            )
            return
