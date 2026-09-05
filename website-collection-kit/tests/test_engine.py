from __future__ import annotations

import asyncio
import gzip

import pytest

from website_collection_kit import (
    Budget,
    CollectionIntent,
    CollectionSpec,
    CollectionStatus,
    InspectionSpec,
    InvalidSpecError,
    MemoryEvidenceStore,
    NoAcquisitionCapabilityError,
    RequiredEvidenceUnavailableError,
    Scope,
    WebsiteCollectionKit,
)
from website_collection_kit.ports import FetchRequest, FetchResponse


def _body(value: str) -> bytes:
    return value.encode("utf-8")


class FakeAcquisition:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.requests: list[FetchRequest] = []

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        self.requests.append(request)
        return self.responses.get(
            request.url,
            FetchResponse(
                request.url,
                request.url,
                404,
                {"content-type": "text/html"},
                b"",
                "text/html",
                "fake",
                1,
            ),
        )


def _response(url: str, body: str, media_type: str = "text/html") -> FetchResponse:
    return FetchResponse(
        url, url, 200, {"content-type": media_type}, _body(body), media_type, "fake", 1
    )


@pytest.mark.asyncio
async def test_collect_site_discovers_sitemap_html_data_and_onclick_links() -> None:
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt",
            "User-agent: *\nSitemap: https://example.test/sitemap.xml\n",
            "text/plain",
        ),
        "https://example.test/sitemap.xml": _response(
            "https://example.test/sitemap.xml",
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/news/1</loc></url></urlset>',
            "application/xml",
        ),
        "https://example.test/": _response(
            "https://example.test/",
            '<html><head><title>官网</title></head><body><main><a href="/news/">新闻</a><a data-url="/about">关于</a><button onclick="location.href=\'/notice/1\'">通知</button><a href="https://outside.test/x">外站</a></main></body></html>',
        ),
        "https://example.test/news/": _response(
            "https://example.test/news/",
            '<html><head><title>新闻动态</title></head><body><main><h1>新闻动态</h1><ul><li><a href="/news/1">1</a></li><li><a href="/news/2">2</a></li><li><a href="/news/3">3</a></li><li><a href="/news/4">4</a></li><li><a href="/news/5">5</a></li><li><a href="/news/6">6</a></li></ul></main></body></html>',
        ),
        "https://example.test/news/1": _response(
            "https://example.test/news/1",
            '<html><head><title>新闻1</title><meta property="article:published_time" content="2026-09-03"/></head><body><article><p>这是新闻正文内容，这是新闻正文内容，这是新闻正文内容，这是新闻正文内容，这是新闻正文内容。</p></article></body></html>',
        ),
        "https://example.test/about": _response(
            "https://example.test/about",
            "<html><head><title>机构概况</title></head><body><article>机构介绍</article></body></html>",
        ),
        "https://example.test/notice/1": _response(
            "https://example.test/notice/1",
            "<html><head><title>通知公告</title></head><body><article>通知正文</article></body></html>",
        ),
    }
    acquisition = FakeAcquisition(responses)
    kit = WebsiteCollectionKit(acquisition, evidence=MemoryEvidenceStore())
    scope = Scope.for_seeds(["https://example.test/"])
    spec = CollectionSpec(
        "collection-1",
        "site-1",
        CollectionIntent.SITE_SWEEP,
        scope,
        seeds=("https://example.test/",),
        budget=Budget(
            max_pages=20, max_candidates=100, max_depth=3, max_duration_seconds=10
        ),
    )

    result = await kit.collect_site(spec)

    assert (
        result.status is CollectionStatus.PARTIAL
    )  # sitemap links 2..6 are deliberate 404s
    assert {page.url for page in result.pages} == {
        "https://example.test/",
        "https://example.test/news/",
        "https://example.test/news/1",
        "https://example.test/about",
        "https://example.test/notice/1",
    }
    assert result.coverage.discovery_sources_succeeded == (
        "explicit_seed",
        "robots",
        "sitemap",
        "html_link",
        "data_url",
        "onclick",
    )
    assert any("outside.test" in url for url in result.pages[0].outbound_sources)
    assert all(page.evidence_refs for page in result.pages)
    assert result.to_payload()["schema_type"] == "website.collection@1.0"


@pytest.mark.asyncio
async def test_discovery_excludes_private_endpoints_but_keeps_public_json() -> None:
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
        ),
        "https://example.test/": _response(
            "https://example.test/",
            """
            <html><body><main>
              <a href="/login">登录</a>
              <a href="/api/Web/Member/GetMemberInfo">会员接口</a>
              <a href="/api/GetMemberInfo">另一会员接口</a>
              <a href="/api/getmemberinfo">小写会员接口</a>
              <a href="/api/Web/Order/GetPayStatus">支付状态</a>
              <a href="/feedbackService/insert">反馈提交</a>
              <a href="/api/news/list">公开新闻数据</a>
              <a href="/article/1">文章一</a>
              <a href="/static/upload/demo.mp4">视频附件</a>
            </main></body></html>
            """,
        ),
        "https://example.test/api/news/list": FetchResponse(
            "https://example.test/api/news/list",
            "https://example.test/api/news/list",
            200,
            {"content-type": "application/json"},
            b'{"url":"/article/2"}',
            "application/json",
            "fake",
            1,
        ),
        "https://example.test/article/1": _response(
            "https://example.test/article/1", "<html><body><article>文章一正文</article></body></html>"
        ),
        "https://example.test/article/2": _response(
            "https://example.test/article/2", "<html><body><article>文章二正文</article></body></html>"
        ),
        "https://example.test/static/upload/demo.mp4": FetchResponse(
            "https://example.test/static/upload/demo.mp4",
            "https://example.test/static/upload/demo.mp4",
            200,
            {"content-type": "video/mp4"},
            b"video",
            "video/mp4",
            "fake",
            1,
        ),
    }
    acquisition = FakeAcquisition(responses)

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "endpoint-filter",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(max_pages=10, max_candidates=20, max_depth=2, max_duration_seconds=10),
        )
    )

    page_urls = {page.url for page in result.pages}
    assert "https://example.test/api/news/list" in page_urls
    assert "https://example.test/article/2" in page_urls
    assert "https://example.test/static/upload/demo.mp4" in page_urls
    assert not any("/login" in url for url in page_urls)
    assert not any("Member/GetMemberInfo" in url for url in page_urls)
    assert not any(url.endswith("/api/GetMemberInfo") for url in page_urls)
    assert not any(url.endswith("/api/getmemberinfo") for url in page_urls)
    assert not any("GetPayStatus" in url for url in page_urls)
    assert not any("feedbackService/insert" in url for url in page_urls)
    assert {(item.reason, item.url) for item in result.exclusions} >= {
        ("login_route", "https://example.test/login"),
        ("private_endpoint", "https://example.test/api/Web/Member/GetMemberInfo"),
        ("private_endpoint", "https://example.test/api/Web/Order/GetPayStatus"),
        ("mutating_endpoint", "https://example.test/feedbackService/insert"),
        ("private_endpoint", "https://example.test/api/GetMemberInfo"),
        ("private_endpoint", "https://example.test/api/getmemberinfo"),
    }


@pytest.mark.asyncio
async def test_recall_first_keeps_template_named_public_pages() -> None:
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
        ),
        "https://example.test/": _response(
            "https://example.test/",
            '<html><body><main><a href="/basic-template/public-article">公开内容</a></main></body></html>',
        ),
        "https://example.test/basic-template/public-article": _response(
            "https://example.test/basic-template/public-article",
            "<html><head><title>模板相关公开说明</title></head><body><article>这是实际公开内容页，不应因为路径名称像模板而被漏掉。</article></body></html>",
        ),
    }

    result = await WebsiteCollectionKit(FakeAcquisition(responses)).collect_site(
        CollectionSpec(
            "recall-first",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert "https://example.test/basic-template/public-article" in {
        page.url for page in result.pages
    }
    assert not any(item.reason == "template_route" for item in result.exclusions)


@pytest.mark.asyncio
async def test_previous_navigation_discovers_older_content() -> None:
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
        ),
        "https://example.test/": _response(
            "https://example.test/",
            '<html><body><main><a href="/news/2">最新</a></main></body></html>',
        ),
        "https://example.test/news/2": _response(
            "https://example.test/news/2",
            '<html><head><link rel="prev" href="/news/1"></head><body><article>第二篇正文</article></body></html>',
        ),
        "https://example.test/news/1": _response(
            "https://example.test/news/1",
            '<html><body><article>第一篇正文</article></body></html>',
        ),
    }

    result = await WebsiteCollectionKit(FakeAcquisition(responses)).collect_site(
        CollectionSpec(
            "previous-navigation",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=3, max_duration_seconds=10
            ),
        )
    )

    assert {
        "https://example.test/news/1",
        "https://example.test/news/2",
    }.issubset({page.url for page in result.pages})


@pytest.mark.asyncio
async def test_normal_links_are_visited_before_script_hints_when_budget_is_tight() -> None:
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
        ),
        "https://example.test/": _response(
            "https://example.test/",
            '<html><body><main><a href="/first">第一层</a><script>const hint = "/script/artifact.html";</script></main></body></html>',
        ),
        "https://example.test/first": _response(
            "https://example.test/first",
            '<html><body><main><a href="/normal-second">正常页面</a></main></body></html>',
        ),
        "https://example.test/normal-second": _response(
            "https://example.test/normal-second",
            "<html><body><article>预算紧张时也应优先访问的真实内容</article></body></html>",
        ),
    }

    result = await WebsiteCollectionKit(
        FakeAcquisition(responses), max_parallel_fetches=1
    ).collect_site(
        CollectionSpec(
            "frontier-priority",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=3, max_candidates=20, max_depth=3, max_duration_seconds=10
            ),
        )
    )

    page_urls = {page.url for page in result.pages}
    assert "https://example.test/normal-second" in page_urls
    assert "https://example.test/script/artifact.html" not in page_urls
    assert result.coverage.candidate_count == 4
    assert not result.coverage.frontier_converged


@pytest.mark.asyncio
async def test_gzip_sitemap_is_traversed_without_entry_truncation() -> None:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>https://example.test/gzip-article</loc></url></urlset>'
    ).encode("utf-8")
    responses = {
        "https://example.test/robots.txt": _response(
            "https://example.test/robots.txt",
            "User-agent: *\nSitemap: https://example.test/sitemap.xml.gz\n",
            "text/plain",
        ),
        "https://example.test/sitemap.xml.gz": FetchResponse(
            "https://example.test/sitemap.xml.gz",
            "https://example.test/sitemap.xml.gz",
            200,
            {"content-type": "application/gzip"},
            gzip.compress(sitemap),
            "application/gzip",
            "fake",
            1,
        ),
        "https://example.test/": _response(
            "https://example.test/", "<html><body><main>首页</main></body></html>"
        ),
        "https://example.test/gzip-article": _response(
            "https://example.test/gzip-article",
            "<html><body><article>压缩 sitemap 发现的正文</article></body></html>",
        ),
    }

    result = await WebsiteCollectionKit(FakeAcquisition(responses)).collect_site(
        CollectionSpec(
            "gzip-sitemap",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert "https://example.test/gzip-article" in {
        page.url for page in result.pages
    }


@pytest.mark.asyncio
async def test_refresh_pages_obeys_robots_but_does_not_follow_new_links() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/fix": _response(
                "https://example.test/fix",
                '<html><head><title>复查</title></head><body><a href="/new">新页面</a><article>已整改内容</article></body></html>',
            ),
        }
    )
    kit = WebsiteCollectionKit(acquisition)
    scope = Scope.for_seeds(["https://example.test/"])

    result = await kit.refresh_pages(
        CollectionSpec(
            "refresh-1",
            "site-1",
            CollectionIntent.TARGETED_REFRESH,
            scope,
            targets=("https://example.test/fix",),
            budget=Budget(
                max_pages=2, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert result.status is CollectionStatus.COMPLETED
    assert len(result.pages) == 1
    assert [request.url for request in acquisition.requests] == [
        "https://example.test/robots.txt",
        "https://example.test/fix",
    ]


@pytest.mark.asyncio
async def test_redirect_is_rechecked_against_scope() -> None:
    redirect = FetchResponse(
        "https://example.test/",
        "https://example.test/",
        302,
        {"location": "https://outside.test/"},
        b"",
        "text/html",
        "fake",
        1,
        redirect_to="https://outside.test/",
    )
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": redirect,
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "redirect-1",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2, max_candidates=5, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert not result.pages
    assert any(item.reason == "redirect_out_of_scope" for item in result.exclusions)
    assert result.coverage.frontier_converged


@pytest.mark.asyncio
async def test_inspection_returns_profile_draft_and_coverage() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><head><title>Home</title></head><body><a href="/about">About</a></body></html>',
            ),
            "https://example.test/about": _response(
                "https://example.test/about",
                "<html><head><title>About</title></head><body><article>About us</article></body></html>",
            ),
        }
    )
    scope = Scope.for_seeds(["https://example.test/"])
    result = await WebsiteCollectionKit(acquisition).inspect_site(
        InspectionSpec("inspect-1", "site-1", ("https://example.test/",), scope)
    )

    assert result.profile_draft.site_ref == "site-1"
    assert result.route_families
    assert result.coverage.stop_reason.value == "converged"


@pytest.mark.asyncio
async def test_html_discovery_reads_sitemap_feed_and_json_url_hints() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><head><title>Home</title><link rel="sitemap" href="/inline-sitemap.xml"><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body><script>fetch("/api/news")</script></body></html>',
            ),
            "https://example.test/inline-sitemap.xml": _response(
                "https://example.test/inline-sitemap.xml",
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/from-sitemap</loc></url></urlset>',
                "application/xml",
            ),
            "https://example.test/feed.xml": _response(
                "https://example.test/feed.xml",
                "<rss><channel><item><link>https://example.test/from-feed</link></item></channel></rss>",
                "application/xml",
            ),
            "https://example.test/api/news": _response(
                "https://example.test/api/news",
                '{"items":[{"url":"/from-json"}]}',
                "application/json",
            ),
            "https://example.test/from-sitemap": _response(
                "https://example.test/from-sitemap",
                "<html><title>Sitemap page</title><article>Sitemap content</article></html>",
            ),
            "https://example.test/from-feed": _response(
                "https://example.test/from-feed",
                "<html><title>Feed page</title><article>Feed content</article></html>",
            ),
            "https://example.test/from-json": _response(
                "https://example.test/from-json",
                "<html><title>JSON page</title><article>JSON content</article></html>",
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "discovery-sources",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=20, max_candidates=40, max_depth=3, max_duration_seconds=10
            ),
        )
    )

    urls = {page.url for page in result.pages}
    assert {
        "https://example.test/from-sitemap",
        "https://example.test/from-feed",
        "https://example.test/from-json",
    }.issubset(urls)
    assert "sitemap" in result.coverage.discovery_sources_succeeded
    assert "rss" in result.coverage.discovery_sources_succeeded
    assert any(page.media_type == "application/json" for page in result.pages)


@pytest.mark.asyncio
async def test_robots_are_loaded_for_allowed_subdomains_before_visiting() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="https://sub.example.test/private">private</a></body></html>',
            ),
            "https://sub.example.test/robots.txt": _response(
                "https://sub.example.test/robots.txt",
                "User-agent: *\nDisallow: /private\n",
                "text/plain",
            ),
            "https://sub.example.test/private": _response(
                "https://sub.example.test/private", "<html><body>private</body></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "subdomain-robots",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"], allow_subdomains=True),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert "https://sub.example.test/robots.txt" in [
        request.url for request in acquisition.requests
    ]
    assert not any(page.url.endswith("/private") for page in result.pages)
    assert any(item.reason == "robots_disallowed" for item in result.exclusions)


@pytest.mark.asyncio
async def test_conventional_sitemap_is_used_when_robots_has_no_sitemap() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml",
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/orphan</loc></url></urlset>',
                "application/xml",
            ),
            "https://example.test/orphan": _response(
                "https://example.test/orphan", "<html><body>orphan page</body></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "conventional-sitemap",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert [page.url for page in result.pages] == ["https://example.test/orphan"]
    assert "sitemap" in result.coverage.discovery_sources_succeeded


@pytest.mark.asyncio
async def test_missing_conventional_sitemap_does_not_make_collection_partial() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml",
                "<html><body>not a sitemap</body></html>",
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><body>home</body></html>"
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "optional-sitemap",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert result.status is CollectionStatus.COMPLETED
    assert not result.issues
    assert "sitemap_unavailable" in result.coverage.known_blind_spots


@pytest.mark.asyncio
async def test_soft_not_found_page_is_retained_for_later_classification() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml",
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.test/ghost</loc></url></urlset>',
                "application/xml",
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><body><main>首页</main></body></html>"
            ),
            "https://example.test/ghost": _response(
                "https://example.test/ghost",
                "<html><head><title>404 - 页面未找到</title></head><body><main>404 - 页面未找到</main></body></html>",
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "soft-404",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert {
        page.url for page in result.pages
    } == {"https://example.test/", "https://example.test/ghost"}
    assert any(issue.code == "soft_not_found" for issue in result.issues)
    assert result.coverage.failed_count == 0
    assert result.status is CollectionStatus.COMPLETED


@pytest.mark.asyncio
async def test_soft_not_found_page_still_contributes_discovery_links() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/", '<a href="/ghost">entry</a>'
            ),
            "https://example.test/ghost": _response(
                "https://example.test/ghost",
                '<html><head><title>404 - 页面未找到</title></head><body>'
                '<a href="/real-page">real page</a></body></html>',
            ),
            "https://example.test/real-page": _response(
                "https://example.test/real-page", "<article>real page</article>"
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "soft-404-links",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(max_pages=5, max_candidates=10, max_depth=2, max_duration_seconds=10),
        )
    )

    assert {
        page.url for page in result.pages
    } >= {"https://example.test/ghost", "https://example.test/real-page"}


@pytest.mark.asyncio
async def test_interpretation_failure_keeps_fetched_page_and_evidence() -> None:
    class _BrokenInterpreter:
        def interpret(self, response, *, profile, policy):
            raise ValueError("synthetic classifier failure")

    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><body>raw page</body></html>"
            ),
        }
    )
    result = await WebsiteCollectionKit(
        acquisition, interpreter=_BrokenInterpreter()
    ).collect_site(
        CollectionSpec(
            "interpretation-fallback",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2,
                max_candidates=5,
                max_depth=1,
                max_duration_seconds=10,
            ),
        )
    )

    assert result.pages[0].page_type.value == "other"
    assert "raw page" in result.pages[0].body
    assert result.pages[0].evidence_refs
    assert any(issue.code == "interpretation_failed" for issue in result.issues)


@pytest.mark.asyncio
async def test_sitemap_entries_are_only_limited_by_collection_budget() -> None:
    locations = "".join(
        f"<url><loc>https://example.test/page/{index}</loc></url>"
        for index in range(5_001)
    )
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt",
                "User-agent: *\nSitemap: https://example.test/sitemap.xml\n",
                "text/plain",
            ),
            "https://example.test/sitemap.xml": _response(
                "https://example.test/sitemap.xml",
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locations}</urlset>',
                "application/xml",
            ),
        }
    )

    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "sitemap-entry-limit",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=1, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert "sitemap_url_entry_limit" not in result.coverage.known_blind_spots
    assert "max_candidates" in result.coverage.limits_reached


def test_robots_rules_support_wildcards_and_end_anchors() -> None:
    from website_collection_kit.collection import _parse_robots

    rules = _parse_robots(
        b"User-agent: *\nDisallow: /private/*.pdf$\nAllow: /private/public.pdf\n"
    )

    assert not rules.allows("https://example.test/private/report.pdf")
    assert rules.allows("https://example.test/private/report.pdf?download=1")
    assert rules.allows("https://example.test/private/public.pdf")


class _OutOfOrderAcquisition(FakeAcquisition):
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if request.url == "https://example.test/a":
            await asyncio.sleep(0.03)
        elif request.url == "https://example.test/b":
            await asyncio.sleep(0.001)
        return await super().fetch(request)


@pytest.mark.asyncio
async def test_parallel_fetches_commit_discoveries_in_frontier_order() -> None:
    acquisition = _OutOfOrderAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>',
            ),
            "https://example.test/a": _response(
                "https://example.test/a",
                '<html><body><a href="/shared">shared</a></body></html>',
            ),
            "https://example.test/b": _response(
                "https://example.test/b",
                '<html><body><a data-url="/shared">shared</a></body></html>',
            ),
            "https://example.test/shared": _response(
                "https://example.test/shared", "<html><body>shared</body></html>"
            ),
        }
    )
    inspection = await WebsiteCollectionKit(
        acquisition, max_parallel_fetches=2
    ).inspect_site(
        InspectionSpec(
            "ordered-commit-inspection",
            "site-1",
            ("https://example.test/",),
            Scope.for_seeds(["https://example.test/"]),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=3, max_duration_seconds=10
            ),
        )
    )
    shared_candidate = next(
        item for item in inspection.candidates if item.canonical_url.endswith("/shared")
    )
    assert shared_candidate.discovery_sources == ("html_link", "data_url")


@pytest.mark.asyncio
async def test_invalid_url_seed_is_rejected_as_an_invalid_spec() -> None:
    class _UnusedAcquisition:
        async def fetch(self, request: FetchRequest) -> FetchResponse:
            raise AssertionError("invalid seed must not be fetched")

    with pytest.raises(InvalidSpecError, match="usable URL seed"):
        await WebsiteCollectionKit(_UnusedAcquisition()).collect_site(
            CollectionSpec(
                "invalid-seed",
                "site-1",
                CollectionIntent.SITE_SWEEP,
                Scope.for_seeds(["https://example.test/"]),
                seeds=("not a URL",),
            )
        )


@pytest.mark.asyncio
async def test_missing_acquisition_capability_remains_actionable() -> None:
    class _UnavailableAcquisition:
        async def fetch(self, request: FetchRequest) -> FetchResponse:
            raise NoAcquisitionCapabilityError("adapter unavailable")

    with pytest.raises(NoAcquisitionCapabilityError, match="adapter unavailable"):
        await WebsiteCollectionKit(_UnavailableAcquisition()).collect_site(
            CollectionSpec(
                "missing-adapter",
                "site-1",
                CollectionIntent.SITE_SWEEP,
                Scope.for_seeds(["https://example.test/"]),
                seeds=("https://example.test/",),
            )
        )


@pytest.mark.asyncio
async def test_evidence_write_failure_is_a_task_level_error() -> None:
    class _FailingEvidence:
        def save(self, payload) -> None:
            raise OSError("evidence disk is unavailable")

    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><body>page</body></html>"
            ),
        }
    )

    with pytest.raises(RequiredEvidenceUnavailableError, match="evidence disk"):
        await WebsiteCollectionKit(
            acquisition, evidence=_FailingEvidence()
        ).collect_site(
            CollectionSpec(
                "evidence-failure",
                "site-1",
                CollectionIntent.SITE_SWEEP,
                Scope.for_seeds(["https://example.test/"]),
                seeds=("https://example.test/",),
            )
        )


@pytest.mark.asyncio
async def test_candidate_budget_is_reported_when_discovery_produces_too_many_urls() -> (
    None
):
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="/one">1</a><a href="/two">2</a><a href="/three">3</a></body></html>',
            ),
            "https://example.test/one": _response(
                "https://example.test/one", "<html><article>one</article></html>"
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "candidate-budget",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2, max_candidates=2, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert result.coverage.stop_reason.value == "budget_exhausted"
    assert result.coverage.candidate_count == 2
    assert "max_candidates" in result.coverage.limits_reached


@pytest.mark.asyncio
async def test_page_budget_is_reported_with_pending_frontier() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="/one">1</a><a href="/two">2</a></body></html>',
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "page-budget",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=1, max_candidates=10, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert result.coverage.page_count == 1
    assert result.coverage.stop_reason.value == "budget_exhausted"
    assert "max_pages" in result.coverage.limits_reached
    assert result.status is CollectionStatus.PARTIAL


@pytest.mark.asyncio
async def test_depth_budget_is_reported_when_deeper_candidates_are_excluded() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/",
                '<html><body><a href="/deeper">deeper</a></body></html>',
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "depth-budget",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=0, max_duration_seconds=10
            ),
        )
    )

    assert len(result.pages) == 1
    assert result.coverage.stop_reason.value == "budget_exhausted"
    assert "max_depth" in result.coverage.limits_reached
    assert not result.coverage.frontier_converged


class _SlowAcquisition(FakeAcquisition):
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if request.url != "https://example.test/robots.txt":
            await asyncio.sleep(0.05)
        return await super().fetch(request)


@pytest.mark.asyncio
async def test_duration_budget_cancels_a_slow_fetch() -> None:
    acquisition = _SlowAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/": _response(
                "https://example.test/", "<html><article>slow</article></html>"
            ),
        }
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "duration-budget",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/",),
            budget=Budget(
                max_pages=2, max_candidates=10, max_depth=1, max_duration_seconds=0.01
            ),
        )
    )

    assert result.coverage.stop_reason.value == "budget_exhausted"
    assert "max_duration_seconds" in result.coverage.limits_reached
    assert result.status is CollectionStatus.PARTIAL


class _BlockingAcquisition:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = 0

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if request.url == "https://example.test/robots.txt":
            return _response(request.url, "User-agent: *\n", "text/plain")
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("blocking acquisition unexpectedly completed")


@pytest.mark.asyncio
async def test_cancelling_collection_drains_inflight_page_tasks() -> None:
    acquisition = _BlockingAcquisition()
    kit = WebsiteCollectionKit(acquisition)
    task = asyncio.create_task(
        kit.collect_site(
            CollectionSpec(
                "cancelled",
                "site-1",
                CollectionIntent.SITE_SWEEP,
                Scope.for_seeds(["https://example.test/"]),
                seeds=("https://example.test/",),
                budget=Budget(
                    max_pages=2, max_candidates=10, max_depth=1, max_duration_seconds=10
                ),
            )
        )
    )
    await asyncio.wait_for(acquisition.started.wait(), timeout=1)

    task.cancel()
    result = await task

    assert result.status is CollectionStatus.CANCELLED
    assert acquisition.cancelled == 1


@pytest.mark.asyncio
async def test_path_scoped_collection_still_reads_root_robots() -> None:
    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/news/": _response(
                "https://example.test/news/", "<html><article>news</article></html>"
            ),
        }
    )
    await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "path-scope",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(
                ["https://example.test/news/"], allowed_path_prefixes=("/news",)
            ),
            seeds=("https://example.test/news/",),
            budget=Budget(
                max_pages=2, max_candidates=10, max_depth=1, max_duration_seconds=10
            ),
        )
    )

    assert acquisition.requests[0].url == "https://example.test/robots.txt"


@pytest.mark.asyncio
async def test_page_redirect_chain_stops_at_redirect_budget() -> None:
    def redirect(url: str, target: str) -> FetchResponse:
        return FetchResponse(
            url,
            url,
            302,
            {"location": target},
            b"",
            "text/html",
            "fake",
            1,
            redirect_to=target,
        )

    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/a": redirect("https://example.test/a", "/b"),
            "https://example.test/b": redirect("https://example.test/b", "/c"),
            "https://example.test/c": redirect("https://example.test/c", "/d"),
        }
    )
    result = await WebsiteCollectionKit(acquisition, max_redirects=2).collect_site(
        CollectionSpec(
            "redirect-budget",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/a",),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=3, max_duration_seconds=10
            ),
        )
    )

    assert not result.pages
    assert any(issue.code == "too_many_redirects" for issue in result.issues)


@pytest.mark.asyncio
async def test_profile_scope_narrows_engine_scope() -> None:
    from website_collection_kit import SiteProfile

    acquisition = FakeAcquisition(
        {
            "https://example.test/robots.txt": _response(
                "https://example.test/robots.txt", "User-agent: *\n", "text/plain"
            ),
            "https://example.test/news/": _response(
                "https://example.test/news/",
                '<html><body><a href="/about">about</a></body></html>',
            ),
        }
    )
    profile = SiteProfile(
        "profile-1",
        "1",
        "site-1",
        scope=Scope.for_seeds(
            ["https://example.test/"], allowed_path_prefixes=("/news",)
        ),
    )
    result = await WebsiteCollectionKit(acquisition).collect_site(
        CollectionSpec(
            "profile-scope",
            "site-1",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds(["https://example.test/"]),
            seeds=("https://example.test/news/",),
            profile=profile,
            budget=Budget(
                max_pages=5, max_candidates=10, max_depth=2, max_duration_seconds=10
            ),
        )
    )

    assert [page.url for page in result.pages] == ["https://example.test/news/"]
    assert any(item.reason == "path_outside_scope" for item in result.exclusions)
