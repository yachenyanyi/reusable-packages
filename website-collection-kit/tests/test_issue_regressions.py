from __future__ import annotations

import socket
from dataclasses import fields
from typing import get_type_hints

import pytest
import website_collection_kit

from website_collection_kit import (
    AcquisitionError,
    Budget,
    CollectionIntent,
    CollectionSpec,
    CollectionStatus,
    EvidenceRef,
    EvidencePort,
    FetchRequest,
    FetchResponse,
    HttpxAcquisition,
    InspectionSpec,
    PublicNetworkPolicy,
    Scope,
    SiteProfile,
    WebsiteCollectionKit,
)
from website_collection_kit.ports import AcquisitionPort
from website_collection_kit.robots import RobotsState, parse_robots
from website_collection_kit.url_policy import UrlPolicy


def _response(
    url: str,
    body: str | bytes = b"",
    media_type: str = "text/html",
    *,
    status: int = 200,
    redirect_to: str | None = None,
) -> FetchResponse:
    content = body.encode("utf-8") if isinstance(body, str) else body
    return FetchResponse(
        requested_url=url,
        final_url=url,
        status=status,
        headers={"content-type": media_type},
        body=content,
        media_type=media_type,
        fetch_method="fake",
        elapsed_ms=1,
        redirect_to=redirect_to,
    )


class _RecordingAcquisition:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.requests: list[FetchRequest] = []
        self.responses_returned: list[FetchResponse] = []

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        response = self.responses.get(
            request.url,
            _response(request.url, b"", "text/html", status=404),
        )
        self.responses_returned.append(response)
        return response


def _site_spec(
    collection_id: str,
    scope: Scope,
    *,
    seeds: tuple[str, ...],
    budget: Budget | None = None,
) -> CollectionSpec:
    return CollectionSpec(
        collection_id,
        "site-1",
        CollectionIntent.SITE_SWEEP,
        scope,
        seeds=seeds,
        budget=budget
        or Budget(max_pages=10, max_candidates=20, max_depth=2, max_duration_seconds=10),
    )


def test_query_scope_intersection_has_explicit_three_state_semantics() -> None:
    unrestricted = Scope.for_seeds(["https://example.test/"])
    restricted = Scope.for_seeds(
        ["https://example.test/"], allowed_query_keys=("id",)
    )
    effective = unrestricted.restrict_with(restricted)

    assert effective.allowed_query_keys == ("id",)
    policy = UrlPolicy(effective)
    assert policy.decide("https://example.test/page?id=1").accepted
    assert policy.decide("https://example.test/page?token=secret").reason == (
        "query_key_not_allowed"
    )


def test_empty_query_intersection_does_not_turn_back_into_unrestricted() -> None:
    left = Scope.for_seeds(
        ["https://example.test/"], allowed_query_keys=("id",)
    )
    right = Scope.for_seeds(
        ["https://example.test/"], allowed_query_keys=("page",)
    )
    effective = left.restrict_with(right)
    policy = UrlPolicy(effective)

    assert effective.allowed_query_keys == ()
    assert policy.decide("https://example.test/page").accepted
    for query in ("id=1", "page=1", "token=secret"):
        assert policy.decide(f"https://example.test/page?{query}").reason == (
            "query_key_not_allowed"
        )


def test_scope_intersection_intersects_ignored_query_keys_without_widening() -> None:
    left = Scope.for_seeds(
        ["https://example.test/"],
        allowed_query_keys=(),
        ignored_query_keys=("tracking",),
    )
    right = Scope.for_seeds(
        ["https://example.test/"],
        allowed_query_keys=(),
        ignored_query_keys=(),
    )
    effective = left.restrict_with(right)

    assert effective.ignored_query_keys == ()
    assert not UrlPolicy(effective).decide(
        "https://example.test/page?tracking=mail"
    ).accepted


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test/news/2026?id=1",
        "https://example.test/news/2026?page=1",
        "https://example.test/about?id=1",
        "https://other.test/news/2026?id=1",
        "http://example.test/news/2026?id=1",
    ),
)
def test_scope_intersection_never_accepts_a_url_rejected_by_an_input_scope(
    url: str,
) -> None:
    left = Scope.for_seeds(
        ["https://example.test/"],
        allowed_path_prefixes=("/news",),
        allowed_query_keys=("id", "page"),
    )
    right = Scope.for_seeds(
        ["https://example.test/news/2026"],
        allowed_path_prefixes=("/news/2026",),
        allowed_query_keys=("id",),
    )
    effective = UrlPolicy(left.restrict_with(right))

    if url == "https://example.test/news/2026?id=1":
        assert effective.decide(url).accepted
    if effective.decide(url).accepted:
        assert UrlPolicy(left).decide(url).accepted
        assert UrlPolicy(right).decide(url).accepted


def test_url_identity_does_not_rewrite_the_wire_query() -> None:
    policy = UrlPolicy(Scope.for_seeds(["https://example.test/"]))
    raw = "https://example.test/news?b=2&utm_source=mail&a=1&tag=hello%20world#top"

    decision = policy.decide(raw)

    assert decision.accepted
    assert decision.request_url == raw.removesuffix("#top")
    assert decision.canonical_url == (
        "https://example.test/news?a=1&b=2&tag=hello+world"
    )


def test_encoded_and_unicode_paths_share_identity_without_decoding_reserved_slashes() -> None:
    policy = UrlPolicy(Scope.for_seeds(["https://example.test/"]))
    unicode_decision = policy.decide("https://example.test/中文/%2F/%25")
    encoded_decision = policy.decide(
        "https://example.test/%E4%B8%AD%E6%96%87/%2F/%25"
    )

    assert unicode_decision.accepted and encoded_decision.accepted
    assert unicode_decision.canonical_url == encoded_decision.canonical_url
    assert unicode_decision.request_url.endswith("/中文/%2F/%25")
    assert "/%2F/%25" in encoded_decision.request_url
    assert "/%2F/%25" in unicode_decision.canonical_url


@pytest.mark.asyncio
async def test_tracking_aliases_are_retained_while_one_canonical_page_is_fetched() -> None:
    first = "https://example.test/news?utm_source=first"
    second = "https://example.test/news?utm_source=second"
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            first: _response(first, "<html><body><article>news</article></body></html>"),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "canonical-aliases",
            Scope.for_seeds(["https://example.test/"]),
            seeds=(first, second),
        )
    )

    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.url == first
    assert page.canonical_url == "https://example.test/news"
    assert page.request_aliases == (second,)
    assert result.coverage.duplicate_count >= 1
    assert second not in [request.url for request in acquisition.requests]


@pytest.mark.asyncio
async def test_public_network_policy_rejects_non_public_destinations() -> None:
    policy = PublicNetworkPolicy()

    for url in (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://192.168.1.10/",
        "http://169.254.10.20/",
        "http://metadata.google.internal/",
    ):
        decision = await policy.check_url(url)
        assert not decision.accepted
        assert decision.reason == "private_network"

    allowed = await policy.check_url(
        "http://127.0.0.1/", allow_private_network=True
    )
    assert allowed.accepted


@pytest.mark.asyncio
async def test_public_network_policy_rejects_dns_that_resolves_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import website_collection_kit.network as network

    def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            )
        ]

    monkeypatch.setattr(network.socket, "getaddrinfo", fake_getaddrinfo)

    decision = await PublicNetworkPolicy().check_url("https://internal.example/")

    assert not decision.accepted
    assert decision.reason == "private_network"


def test_scope_origin_does_not_widen_to_other_ports_or_schemes() -> None:
    policy = UrlPolicy(Scope.for_seeds(["https://example.test:8443/"]))

    assert policy.decide("https://example.test:8443/page").accepted
    assert policy.decide("https://example.test/page").reason == "external_origin"
    assert policy.decide("http://example.test:8443/page").reason == (
        "external_origin"
    )


def test_scope_preserves_explicit_zero_port() -> None:
    scope = Scope.for_seeds(["http://example.test:0/"])
    policy = UrlPolicy(scope)

    assert policy.decide("http://example.test:0/page").accepted
    assert policy.decide("http://example.test/page").reason == "external_origin"


@pytest.mark.asyncio
async def test_httpx_adapter_rejects_private_network_before_opening_transport() -> None:
    acquisition = HttpxAcquisition(timeout_seconds=1)

    with pytest.raises(AcquisitionError, match="private_network"):
        await acquisition.fetch(FetchRequest("http://127.0.0.1/"))
    await acquisition.close()


@pytest.mark.asyncio
async def test_robots_exclusions_do_not_consume_page_budget() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt",
                "User-agent: *\nDisallow: /blocked\n",
                "text/plain",
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="/blocked-one">one</a>'
                '<a href="/blocked-two">two</a>'
                '<a href="/allowed">allowed</a></body></html>',
            ),
            "https://example.test/allowed": _response(
                "https://example.test/allowed", "<html><article>allowed</article></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "robots-budget",
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2,
                max_candidates=10,
                max_depth=1,
                max_duration_seconds=10,
            ),
        )
    )

    assert {page.url for page in result.pages} == {
        "https://example.test/",
        "https://example.test/allowed",
    }
    assert result.coverage.visited_count == 2
    assert all("blocked" not in request.url for request in acquisition.requests)


@pytest.mark.asyncio
async def test_retry_counts_attempts_but_failed_page_is_unique() -> None:
    target = "https://example.test/fails"

    class _RetryingAcquisition(_RecordingAcquisition):
        def __init__(self) -> None:
            super().__init__(
                {
                    "https://example.test/robots.txt": _response(
                        "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
                    )
                }
            )
            self.target_attempts = 0

        async def fetch(self, request: FetchRequest) -> FetchResponse:
            self.requests.append(request)
            if request.url == target:
                self.target_attempts += 1
                raise AcquisitionError("temporary", "temporary failure", retryable=True)
            response = self.responses.get(
                request.url,
                _response(request.url, b"", "text/html", status=404),
            )
            self.responses_returned.append(response)
            return response

    acquisition = _RetryingAcquisition()
    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "retry-accounting",
            Scope.for_seeds(["https://example.test/"]),
            seeds=(target,),
            budget=Budget(
                max_pages=2,
                max_candidates=10,
                max_depth=1,
                max_duration_seconds=10,
                max_requests=20,
            ),
        )
    )

    assert acquisition.target_attempts == 2
    assert result.coverage.failed_count == 1
    assert result.usage.failed_pages == 1
    assert result.usage.requests == len(acquisition.requests)
    assert result.status is CollectionStatus.FAILED


@pytest.mark.asyncio
async def test_usage_includes_auxiliary_responses_and_all_received_bytes() -> None:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>https://example.test/from-sitemap</loc></url></urlset>'
    )
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt",
                "User-agent: *\nSitemap: https://example.test/sitemap.xml\n",
                "text/plain",
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml", sitemap, "application/xml"
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><article>home</article></html>"
            ),
            "https://example.test/from-sitemap": _response(
                "https://example.test/from-sitemap",
                "<html><article>article</article></html>",
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "usage-accounting",
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
        )
    )

    assert result.usage.requests == len(acquisition.requests)
    assert result.usage.bytes_received == sum(
        len(response.body) for response in acquisition.responses_returned
    )
    assert result.coverage.candidate_count >= result.coverage.page_count


@pytest.mark.asyncio
async def test_request_budget_stops_auxiliary_discovery() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            )
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "request-budget",
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2,
                max_candidates=10,
                max_depth=1,
                max_duration_seconds=10,
                max_requests=1,
            ),
        )
    )

    assert result.coverage.stop_reason.value == "budget_exhausted"
    assert "max_requests" in result.coverage.limits_reached
    assert result.usage.requests == 1
    assert not result.pages


@pytest.mark.asyncio
async def test_request_budget_rejection_is_not_a_failed_page_or_a_visit() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/": _response(
                "https://example.test/", "<html><article>home</article></html>"
            )
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "request-budget-page",
            Scope.for_seeds(["https://example.test/"], respect_robots=False),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2,
                max_candidates=10,
                max_depth=1,
                max_duration_seconds=10,
                max_requests=6,
            ),
        )
    )

    assert not result.pages
    assert result.coverage.visited_count == 0
    assert result.coverage.failed_count == 0
    assert result.usage.failed_pages == 0
    assert result.status is CollectionStatus.PARTIAL
    assert result.usage.requests == 6


@pytest.mark.asyncio
async def test_rendered_page_budget_is_bounded_without_counting_budget_as_page_failure() -> None:
    first = "https://example.test/first"
    second = "https://example.test/second"

    class _RenderedAcquisition(_RecordingAcquisition):
        async def fetch(self, request: FetchRequest) -> FetchResponse:
            self.requests.append(request)
            response = _response(
                request.url,
                "<html><article>rendered</article></html>",
            )
            response = FetchResponse(
                response.requested_url,
                response.final_url,
                response.status,
                response.headers,
                response.body,
                response.media_type,
                "playwright",
                response.elapsed_ms,
            )
            self.responses_returned.append(response)
            return response

    acquisition = _RenderedAcquisition({})
    result = await WebsiteCollectionKit(acquisition).refresh_pages(
        CollectionSpec(
            "rendered-budget",
            "site-1",
            CollectionIntent.TARGETED_REFRESH,
            Scope.for_seeds(["https://example.test/"], respect_robots=False),
            targets=(first, second),
            budget=Budget(
                max_pages=2,
                max_candidates=10,
                max_depth=1,
                max_duration_seconds=10,
                max_rendered_pages=1,
            ),
        )
    )

    assert len(result.pages) == 1
    assert result.coverage.visited_count == 2
    assert result.coverage.failed_count == 0
    assert result.usage.rendered_pages == 2
    assert "max_rendered_pages" in result.coverage.limits_reached
    assert result.status is CollectionStatus.PARTIAL


def test_robots_parser_selects_specific_group_and_merges_matches() -> None:
    rules = parse_robots(
        b"""
        User-agent: *
        Disallow: /

        User-agent: website-collection-kit
        User-agent: AnotherBot
        Disallow: /private
        Allow: /private/public

        User-agent: WEBSITE-COLLECTION-KIT
        Disallow: /secret
        """,
        product_token="website-collection-kit",
    )

    assert rules.state is RobotsState.AVAILABLE
    assert rules.allows("https://example.test/")
    assert not rules.allows("https://example.test/private")
    assert rules.allows("https://example.test/private/public")
    assert not rules.allows("https://example.test/secret")


def test_robots_allow_wins_when_matching_rule_lengths_are_equal() -> None:
    rules = parse_robots(
        b"User-agent: *\nDisallow: /private/page\nAllow: /private/page\n"
    )

    assert rules.allows("https://example.test/private/page")


@pytest.mark.asyncio
async def test_unreachable_robots_is_fail_closed_and_reported_as_a_blind_spot() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt",
                "server error",
                "text/plain",
                status=500,
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><article>must not fetch</article></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "robots-500",
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
        )
    )

    assert not result.pages
    assert any(issue.code == "robots_unreachable" for issue in result.issues)
    assert "robots_unreachable" in result.coverage.known_blind_spots
    assert any(item.reason == "robots_disallowed" for item in result.exclusions)
    assert "https://example.test/" not in [request.url for request in acquisition.requests]


@pytest.mark.asyncio
async def test_respect_robots_false_explicitly_skips_robots_enforcement() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "server error", "text/plain", status=500
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><article>allowed</article></html>"
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "robots-skipped",
            Scope.for_seeds(["https://example.test/"], respect_robots=False),
            seeds=("https://example.test/",),
        )
    )

    assert [page.url for page in result.pages] == ["https://example.test/"]
    assert not any(issue.code == "robots_unreachable" for issue in result.issues)
    assert "https://example.test/robots.txt" not in [
        request.url for request in acquisition.requests
    ]


@pytest.mark.asyncio
async def test_declared_invalid_sitemap_is_an_issue_but_guessed_sitemap_is_optional() -> None:
    acquisition = _RecordingAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt",
                "User-agent: *\nSitemap: https://example.test/declared.xml\n",
                "text/plain",
            ),
            "https://example.test/declared.xml": _response(
                "https://example.test/declared.xml", "<html>not xml sitemap</html>"
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml", "<html>optional probe miss</html>"
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><article>home</article></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        _site_spec(
            "sitemap-provenance",
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
        )
    )

    assert any(
        issue.code == "invalid_sitemap"
        and issue.url == "https://example.test/declared.xml"
        for issue in result.issues
    )
    assert not any(
        issue.code == "invalid_sitemap"
        and issue.url == "https://example.test/sitemap.xml"
        for issue in result.issues
    )
    assert result.pages


def test_v01_public_contract_has_no_unimplemented_placeholders() -> None:
    assert "schema_version" not in {field.name for field in fields(InspectionSpec)}
    assert "schema_version" not in {field.name for field in fields(CollectionSpec)}
    assert "registered_strategy_refs" not in {
        field.name for field in fields(SiteProfile)
    }
    assert "OperationConflictError" not in website_collection_kit.__all__
    assert get_type_hints(EvidencePort.save)["return"] is EvidenceRef
    assert [field.name for field in fields(Budget)] == [
        "max_pages",
        "max_candidates",
        "max_depth",
        "max_duration_seconds",
        "max_requests",
        "max_total_bytes",
        "max_rendered_pages",
    ]
    assert "fetch" in AcquisitionPort.__dict__
    assert "cancel" not in AcquisitionPort.__dict__
