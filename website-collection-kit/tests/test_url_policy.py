from website_collection_kit import Scope
from website_collection_kit.url_policy import UrlPolicy


def test_canonical_url_removes_tracking_and_sorts_query() -> None:
    policy = UrlPolicy(
        Scope.for_seeds(["https://example.test/"], allowed_path_prefixes=["/news"])
    )

    decision = policy.decide(
        "/news/1?utm_source=x&b=2&a=1#fragment", "https://example.test/"
    )

    assert decision.accepted
    assert decision.canonical_url == "https://example.test/news/1?a=1&b=2"


def test_canonical_url_preserves_key_only_query_routes() -> None:
    policy = UrlPolicy(Scope.for_seeds(["https://example.test/"]))

    key_only = policy.decide("https://example.test/ky-source?abc-123")
    empty_value = policy.decide("https://example.test/ky-source?abc-123=")

    assert key_only.canonical_url == "https://example.test/ky-source?abc-123"
    assert empty_value.canonical_url == "https://example.test/ky-source?abc-123="
    assert key_only.canonical_url != empty_value.canonical_url


def test_path_prefix_is_not_a_string_prefix() -> None:
    policy = UrlPolicy(
        Scope.for_seeds(["https://example.test/"], allowed_path_prefixes=["/news"])
    )

    assert policy.decide("https://example.test/news/1").accepted
    assert not policy.decide("https://example.test/newsroom/1").accepted
    assert policy.decide("https://example.test/news").accepted


def test_external_and_unsupported_urls_are_rejected() -> None:
    policy = UrlPolicy(Scope.for_seeds(["https://example.test/"]))

    assert policy.decide("https://other.test/page").reason == "external_host"
    assert (
        policy.decide("mailto:person@example.test").reason
        == "unsupported_scheme_or_missing_host"
    )


def test_profile_scope_can_only_narrow_operation_scope() -> None:
    outer = Scope.for_seeds(["https://example.test/"])
    inner = Scope.for_seeds(
        ["https://example.test/news"], allowed_path_prefixes=["/news"]
    )
    effective = outer.restrict_with(inner)
    policy = UrlPolicy(effective)

    assert policy.decide("https://example.test/news/1").accepted
    assert not policy.decide("https://example.test/about").accepted


def test_subdomain_profile_intersection_is_not_rejected() -> None:
    outer = Scope.for_seeds(["https://example.test/"], allow_subdomains=True)
    inner = Scope.for_seeds(["https://research.example.test/"])

    effective = outer.restrict_with(inner)

    assert effective.allowed_hosts == ("research.example.test",)
    assert not effective.allow_subdomains
    assert UrlPolicy(effective).decide("https://research.example.test/page").accepted


def test_malformed_url_is_rejected_without_raising() -> None:
    decision = UrlPolicy(Scope.for_seeds(["https://example.test/"])).decide(
        "https://[bad"
    )

    assert not decision.accepted
    assert decision.reason == "unsupported_scheme_or_missing_host"


def test_ipv6_host_keeps_brackets_during_canonicalization() -> None:
    policy = UrlPolicy(Scope.for_seeds(["http://[::1]:8080/"]))

    decision = policy.decide("http://[::1]:8080/page")

    assert decision.accepted
    assert decision.canonical_url == "http://[::1]:8080/page"


def test_route_pattern_accepts_wire_value_and_fetch_request_normalises_rendering() -> (
    None
):
    from website_collection_kit import FetchRequest, RenderingRequirement, RoutePattern

    assert RoutePattern("/news/", "news_list").page_type.value == "news_list"
    assert (
        FetchRequest(" https://example.test/ ", rendering="preferred").rendering
        is RenderingRequirement.PREFERRED
    )
