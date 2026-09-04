import pytest

from website_collection_kit import PageType, Scope, SiteProfile
from website_collection_kit.interpretation import HtmlInterpreter
from website_collection_kit.ports import FetchResponse
from website_collection_kit.url_policy import UrlPolicy


def test_interpreter_extracts_content_dates_links_and_classification() -> None:
    html = """
    <html><head>
      <title>通知公告</title>
      <meta property="article:published_time" content="2026-09-03T10:00:00Z">
      <meta property="og:description" content="通知摘要">
      <link rel="canonical" href="/notice/1?utm_source=menu">
      <link rel="next" href="/notice/next">
    </head><body>
      <nav><a href="/">首页</a></nav>
      <div class="breadcrumb">首页 &gt; 通知公告</div>
      <article id="content">
        <h1>通知公告</h1>
        <p>这是公开通知正文，用于验证正文区域、发布日期和页面类型的提取。</p>
        <a data-url="/notice/2">下一条</a>
        <button onclick="location.href='/about'">关于</button>
        <a href="/docs/notice.pdf">附件</a>
        <a href="https://outside.test/source">来源</a>
      </article>
    </body></html>
    """.encode()
    response = FetchResponse(
        requested_url="https://example.test/notice/1",
        final_url="https://example.test/notice/1",
        status=200,
        headers={"content-type": "text/html; charset=utf-8"},
        body=html,
        media_type="text/html",
        fetch_method="http",
        elapsed_ms=4,
    )
    scope = Scope.for_seeds(["https://example.test/"])
    result = HtmlInterpreter().interpret(
        response, profile=None, policy=UrlPolicy(scope)
    )

    assert result.title == "通知公告"
    assert result.summary == "通知摘要"
    assert result.page_type is PageType.NOTICE
    assert "通知正文" in result.body
    assert result.published_at is not None
    assert result.canonical_url == "https://example.test/notice/1"
    assert {link.raw_url for link in result.links} >= {
        "/notice/2",
        "/notice/next",
        "/about",
        "/docs/notice.pdf",
        "https://outside.test/source",
    }
    assert result.attachments[0].url == "/docs/notice.pdf"
    assert result.section == "首页 > 通知公告"


def test_interpreter_uses_profile_hints_and_does_not_confuse_research_with_search() -> (
    None
):
    html = """
    <html><head><title>关于我们</title></head><body>
      <div id="custom-body">机构概况与研究方向</div>
    </body></html>
    """.encode()
    response = FetchResponse(
        "https://example.test/about",
        "https://example.test/about",
        200,
        {"content-type": "text/html"},
        html,
        "text/html",
        "http",
        1,
    )
    profile = SiteProfile(
        "p1",
        "1",
        "site1",
        field_hints=__import__("website_collection_kit").FieldHints(
            body_selectors=("#custom-body",)
        ),
    )
    result = HtmlInterpreter().interpret(
        response,
        profile=profile,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.PROFILE
    assert result.body == "机构概况与研究方向"


def test_interpreter_reads_dates_from_json_ld_graph() -> None:
    response = FetchResponse(
        "https://example.test/news/1",
        "https://example.test/news/1",
        200,
        {"content-type": "text/html"},
        b'<html><head><script type="application/ld+json">{"@graph":[{"datePublished":"2026-09-03"}]}</script></head><body><article>content</article></body></html>',
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.published_at is not None
    assert result.published_at.year == 2026


def test_interpreter_prioritizes_detail_route_over_global_navigation_words() -> None:
    html = """
    <html><head><title>研究院官网</title></head><body>
      <header><a href="/contact">联系我们</a><a href="/login">登录</a></header>
      <main class="article-detail">
        <h1 class="title">研究院召开技术交流会</h1>
        <div class="content">
          <p>这是新闻详情页的正文内容，包含足够的公开信息和会议说明。</p>
          <p>发布时间：2026年09月03日</p>
        </div>
      </main>
      <footer>联系我们 登录</footer>
    </body></html>
    """.encode()
    response = FetchResponse(
        "https://example.test/news_info/news/detail/123",
        "https://example.test/news_info/news/detail/123",
        200,
        {"content-type": "text/html; charset=utf-8"},
        html,
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE
    assert result.title == "研究院召开技术交流会"
    assert "联系我们" not in result.body
    assert result.published_at is not None


def test_interpreter_handles_legacy_article_path_and_content_container() -> None:
    html = """
    <html><head><title>机构站点</title></head><body>
      <div class="site-nav"><a href="/contact">联系我们</a><a href="/about">关于我们</a></div>
      <div class="content"><div class="articlecontent">
        <h1 class="arti_title">研究院最新动态</h1>
        <div class="wp_articlecontent">
          <p>这是传统高校 CMS 的文章正文，用于确认 page.htm 详情路由和正文容器。</p>
        </div>
      </div></div>
    </body></html>
    """.encode()
    response = FetchResponse(
        "https://example.test/2026/0901/c19038a581178/page.htm",
        "https://example.test/2026/0901/c19038a581178/page.htm",
        200,
        {"content-type": "text/html; charset=utf-8"},
        html,
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE
    assert result.title == "研究院最新动态"
    assert "传统高校 CMS" in result.body


def test_interpreter_does_not_publish_homepage_item_date() -> None:
    response = FetchResponse(
        "https://example.test/",
        "https://example.test/",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"<html><body><main><h1>Home</h1><p>Latest item 2026-09-03</p></main></body></html>",
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.published_at is None
    assert result.published_at_confidence == 0.0


def test_interpreter_ignores_obvious_mutating_script_endpoints() -> None:
    response = FetchResponse(
        "https://example.test/",
        "https://example.test/",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"""
        <html><body><script>
          const read = "/api/news/list";
          const write = "/api/feedbackService/insert";
        </script></body></html>
        """,
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    urls = {link.raw_url for link in result.links}
    assert "/api/news/list" in urls
    assert "/api/feedbackService/insert" not in urls


def test_interpreter_does_not_treat_window_target_as_a_url() -> None:
    response = FetchResponse(
        "https://example.test/",
        "https://example.test/",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b'<html><body><img data-href="/article/1" data-target="_self"></body></html>',
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    urls = {link.raw_url for link in result.links}
    assert "/article/1" in urls
    assert "_self" not in urls


def test_interpreter_distinguishes_research_route_from_search_route() -> None:
    response = FetchResponse(
        "http://example.test/Research.asp?id=10",
        "http://example.test/Research.asp?id=10",
        200,
        {"content-type": "text/html; charset=gb18030"},
        """
        <html><body><div class="content">
          <h1>研究成果</h1><p>公开研究成果介绍。</p>
        </div></body></html>
        """.encode("gb18030"),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.RESEARCH


def test_interpreter_keeps_legacy_news_index_out_of_detail_route() -> None:
    links = "".join(f'<a href="/news_view.asp?id={index}">News {index}</a>' for index in range(8))
    response = FetchResponse(
        "http://example.test/news.asp?id=1",
        "http://example.test/news.asp?id=1",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"<html><body><main><h1>新闻中心</h1>{links}</main></body></html>".encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.NEWS_LIST


def test_interpreter_recognizes_nested_legacy_news_detail() -> None:
    response = FetchResponse(
        "http://example.test/news/party/57376.html",
        "http://example.test/news/party/57376.html",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"""
        <html><body><nav><a href="/contact">Contact</a></nav>
          <main><h1>Public news detail</h1>
            <p>Published 2026-06-10. This is a public news detail page.</p>
          </main>
        </body></html>
        """,
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


def test_interpreter_uses_detail_metadata_when_a_news_detail_has_related_links() -> None:
    related_links = "".join(f'<a href="/related/{index}">Related {index}</a>' for index in range(8))
    response = FetchResponse(
        "https://example.test/news/27.html",
        "https://example.test/news/27.html",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main>
          <h1 class="title">A specific news headline</h1>
          <p>发布时间：2026-09-04</p>
          <p>这是正文内容。</p>
          {related_links}
        </main></body></html>
        """.encode(),
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


def test_interpreter_recognizes_nested_news_detail_even_without_metadata_labels() -> None:
    related_links = "".join(f'<a href="/related/{index}">Related {index}</a>' for index in range(8))
    response = FetchResponse(
        "https://example.test/news/party/57376.html",
        "https://example.test/news/party/57376.html",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main>
          <h1>国家集成电路青年突击队</h1>
          <p>2026/06/10 这是新闻详情正文。</p>
          {related_links}
        </main></body></html>
        """.encode(),
        "text/html",
        "playwright",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    (
        ("https://example.test/824", "联系方式", PageType.CONTACT),
        ("https://example.test/825", "企业文化", PageType.PROFILE),
        ("https://example.test/826", "荣誉资质", PageType.PROFILE),
        ("https://example.test/827", "招聘信息", PageType.RECRUITMENT),
        ("https://example.test/29972/list.htm", "成果转化", PageType.RESEARCH),
        ("https://example.test/29973/list.htm", "工程转化", PageType.RESEARCH),
        ("https://example.test/30446/list.htm", "研究院概况", PageType.PROFILE),
        ("https://example.test/30810/list.htm", "生物芯片研发方向", PageType.RESEARCH),
        ("https://example.test/30811/list.htm", "仿生器官与器官芯片研发中心", PageType.RESEARCH),
        ("https://example.test/case.html", "落地案例", PageType.PRODUCT),
        ("https://example.test/intelligentEquipment.html", "智能装备", PageType.PRODUCT),
        ("https://example.test/yanfa-14.html", "智能制造标准事业部", PageType.RESEARCH),
        ("https://example.test/AlgorithmCenter", "项目系统", PageType.PRODUCT),
        ("https://example.test/page/platform", "公共服务平台", PageType.RESEARCH),
        ("https://example.test/post/28", "项目活动", PageType.ARTICLE),
        ("https://example.test/list-10.html", "媒体聚焦", PageType.LIST),
        ("https://example.test/hyper-spec", "数据增值服务平台", PageType.PRODUCT),
    ),
)
def test_interpreter_prioritizes_semantic_static_pages_over_navigation_links(
    url: str, title: str, expected: PageType
) -> None:
    navigation = "".join(f'<a href="/nav/{index}">导航 {index}</a>' for index in range(8))
    response = FetchResponse(
        url,
        url,
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><head><title>{title}</title></head><body>
          <div class="site-nav">{navigation}</div>
          <main class="content"><h1>{title}</h1>
            <p>这是一个公开页面，包含机构的具体说明内容，供分类器判断页面语义。</p>
          </main>
        </body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is expected
    assert "导航 0" not in result.body


def test_interpreter_recognizes_legacy_enterprise_view_as_article() -> None:
    related_links = "".join(
        f'<a href="/related/{index}">Related {index}</a>' for index in range(8)
    )
    response = FetchResponse(
        "http://example.test/Enterprise_view.asp?id=1&sub_id=10",
        "http://example.test/Enterprise_view.asp?id=1&sub_id=10",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main>
          <h1>研究所与企业技术交流</h1>
          <p>发布日期：2014-09-05</p>
          <p>这是旧式 CMS 的企业合作详情正文，包含具体项目背景、交流过程和公开结果。</p>
          {related_links}
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE
    assert result.published_at is not None


def test_interpreter_recognizes_parameterized_detail_route() -> None:
    response = FetchResponse(
        "https://example.test/index.php?a=detail&c=about&id=17",
        "https://example.test/index.php?a=detail&c=about&id=17",
        200,
        {"content-type": "text/html; charset=utf-8"},
        """
        <html><body><main class="content">
          <h1>临床前药物递送</h1>
          <p>公开技术详情页，介绍研发目标、技术路径、验证结果和合作方式。</p>
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


def test_interpreter_recognizes_archive_id_as_article() -> None:
    response = FetchResponse(
        "https://example.test/archives/2096",
        "https://example.test/archives/2096",
        200,
        {"content-type": "text/html; charset=utf-8"},
        """
        <html><body><main class="content">
          <h1>科技成果转移服务活动成功举办</h1>
          <p>2015-12-21</p>
          <p>这是归档文章正文，介绍活动背景、参与单位和成果转化情况。</p>
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


def test_interpreter_recognizes_numbered_static_page_with_content() -> None:
    response = FetchResponse(
        "http://example.test/page35",
        "http://example.test/page35",
        200,
        {"content-type": "text/html; charset=utf-8"},
        """
        <html><body><main class="content">
          <h1>机构网站</h1>
          <p>这是编号型 CMS 静态页面，正文介绍平台定位、公共服务、技术能力和应用价值。</p>
          <p>页面还公开服务对象、合作方式、实施条件和典型成果等信息，内容不是只有导航链接或栏目入口。</p>
          <p>This public page contains a substantial description of the institution, its services, technical capabilities, cooperation model, implementation conditions, and representative results for public readers.</p>
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE


def test_interpreter_recognizes_compound_application_detail_as_product() -> None:
    related_links = "".join(
        f'<a href="/related/{index}">Related {index}</a>' for index in range(8)
    )
    response = FetchResponse(
        "http://example.test/apply/7_10",
        "http://example.test/apply/7_10",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main class="content">
          <h1>机器人辅助折弯</h1>
          <p>这是行业应用详情页，介绍设备构成、控制流程、技术优势、实施条件和应用效果。</p>
          {related_links}
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.PRODUCT


def test_interpreter_uses_one_bare_date_for_unknown_detail_with_related_links() -> None:
    related_links = "".join(
        f'<a href="/related/{index}">Related {index}</a>' for index in range(8)
    )
    response = FetchResponse(
        "http://example.test/kt/7187/",
        "http://example.test/kt/7187/",
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main class="content">
          <h1>系统级封装协同优化方案</h1>
          <p>2025/07/03</p>
          <p>这是一个公开技术方案详情页，正文介绍系统架构、协同优化方法、验证过程和应用价值，内容足够完整。</p>
          <p>正文还包含技术路线、实施步骤、测试结果、适用范围以及面向合作方的公开说明，足以与只有导航链接的列表页区分。</p>
          {related_links}
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["http://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE
    assert result.published_at is not None


@pytest.mark.parametrize(
    ("url", "title"),
    (
        ("https://example.test/article-item-29.html", "技术交流活动"),
        ("https://example.test/page-9-39.html", "项目活动详情"),
    ),
)
def test_interpreter_recognizes_common_cms_numbered_detail_routes(
    url: str, title: str
) -> None:
    response = FetchResponse(
        url,
        url,
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><body><main class="content">
          <h1>{title}</h1>
          <p>这是一个公开详情页，正文介绍活动背景、参与单位、技术内容和公开成果。</p>
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is PageType.ARTICLE
    assert "公开详情页" in result.body


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    (
        ("https://example.test/Team.html", "研发团队", PageType.PROFILE),
        ("https://example.test/page/introduction", "技术专家", PageType.PROFILE),
        (
            "https://example.test/resource/what-is-hyper-spectral",
            "了解高光谱",
            PageType.RESEARCH,
        ),
    ),
)
def test_interpreter_classifies_content_pages_from_semantic_titles(
    url: str, title: str, expected: PageType
) -> None:
    response = FetchResponse(
        url,
        url,
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><head><title>{title}</title></head><body>
          <main class="content"><h1>{title}</h1>
            <p>这是公开内容页，介绍机构的团队、专家或技术能力，供访问者了解。</p>
          </main>
        </body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is expected
    assert "公开内容页" in result.body


def test_interpreter_removes_a_nul_filled_response_tail() -> None:
    html = """
    <html><head><title>动态详情</title></head><body>
      <main class="content"><h1>动态详情</h1>
        <p>这是正常的公开正文，尾部之外不应被无效的 NUL 缓冲区污染。</p>
      </main>
    </body></html>
    """.encode()
    response = FetchResponse(
        "https://example.test/news/1",
        "https://example.test/news/1",
        200,
        {"content-type": "text/html; charset=utf-8"},
        html + b"\x00" * 200_000,
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert "\x00" not in result.body
    assert len(result.body) < 300
    assert "正常的公开正文" in result.body


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("https://example.test/?News%2F241.html=", PageType.ARTICLE),
        ("https://example.test/?News%2F=", PageType.NEWS_LIST),
        ("https://example.test/?Overview%2F=", PageType.PROFILE),
        ("https://example.test/?EnJob%2F=", PageType.RECRUITMENT),
        ("https://example.test/?JoinUs%2F=", PageType.CONTACT),
        ("https://example.test/?pages_102%2F=", PageType.ARTICLE),
    ),
)
def test_interpreter_handles_root_query_route_cms_urls(
    url: str, expected: PageType
) -> None:
    response = FetchResponse(
        url,
        url,
        200,
        {"content-type": "text/html; charset=utf-8"},
        """
        <html><body><main class="content">
          <h1>公开内容页</h1>
          <p>这是根路径查询路由返回的公开页面，包含足够的说明内容供分类器判断。</p>
          <p>页面内容来自传统 CMS，用于验证查询键中的路由信息不会被误判成首页。</p>
        </main></body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is expected


def test_interpreter_resolves_links_against_html_base() -> None:
    response = FetchResponse(
        "https://example.test/entry/index.html",
        "https://example.test/entry/index.html",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"""
        <html><head><base href="/published/"></head><body>
          <main><a href="detail/1">detail</a><a href="/root">root</a></main>
        </body></html>
        """,
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert "https://example.test/published/detail/1" in {
        link.raw_url for link in result.links
    }
    assert "https://example.test/root" in {link.raw_url for link in result.links}


def test_interpreter_discovers_embedded_resources_meta_refresh_and_script_pages() -> None:
    response = FetchResponse(
        "https://example.test/",
        "https://example.test/",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"""
        <html><head>
          <meta http-equiv="refresh" content="0; url=/news/1">
        </head><body>
          <iframe src="/article/embedded"></iframe>
          <object data="/files/demo.pdf"></object>
          <script>const next = "/news/script-1";</script>
        </body></html>
        """,
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )
    links = {(link.raw_url, link.source) for link in result.links}

    assert ("/article/embedded", "embedded_url") in links
    assert ("/files/demo.pdf", "embedded_url") in links
    assert ("/news/1", "meta_refresh") in links
    assert ("/news/script-1", "script_url") in links
    assert result.attachments[0].url == "/files/demo.pdf"


def test_interpreter_keeps_navigation_relations_and_all_onclick_targets() -> None:
    response = FetchResponse(
        "https://example.test/news/2",
        "https://example.test/news/2",
        200,
        {"content-type": "text/html; charset=utf-8"},
        """
        <html><head>
          <link rel="prev" href="/news/1">
          <link rel="last" href="/news/99">
        </head><body><main>
          <button onclick="location.href='/news/3'; window.open('/news/4')">继续</button>
          <article>正文内容</article>
        </main></body></html>
        """.encode("utf-8"),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )
    links = {(link.raw_url, link.source) for link in result.links}

    assert ("/news/1", "html_link") in links
    assert ("/news/99", "html_link") in links
    assert ("/news/3", "onclick") in links
    assert ("/news/4", "onclick") in links


def test_interpreter_recovers_internal_detail_routes_from_cms_click_handlers() -> None:
    response = FetchResponse(
        "https://example.test/home",
        "https://example.test/home",
        200,
        {"content-type": "text/html; charset=utf-8"},
        b"""
        <html><body>
          <script>
            const navigatorList = [{"id":"news-column","path":"/news"}];
          </script>
          <div onclick='showDetail("article-1", "", "news-column")'>internal</div>
          <div onclick='showBaseDetail("article-2", "")'>internal</div>
          <div onclick='showDetail("article-3", "https://outside.test/article", "news-column")'>external</div>
        </body></html>
        """,
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )
    links = {(link.raw_url, link.source) for link in result.links}

    assert ("/news?id=article-1", "onclick") in links
    assert ("?id=article-2", "onclick") in links
    assert ("https://outside.test/article", "onclick") in links


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    (
        (
            "https://example.test/achievement?id=164638236562374f8274b-5418-436b-800c-f77f309ccf7f",
            "中心成果",
            PageType.PRODUCT,
        ),
        (
            "https://example.test/ky-source?17544622024253bf218d1-c079-4dbf-a368-2a933b0f7a17",
            "专家团队",
            PageType.PROFILE,
        ),
        ("https://example.test/act/", "Series of Activities", PageType.NEWS_LIST),
    ),
)
def test_interpreter_classifies_dynamic_content_routes(
    url: str, title: str, expected: PageType
) -> None:
    body_text = (
        "技术成果类别：公开内容。这里是一个可供监测的公开页面正文。"
        if expected is PageType.PRODUCT
        else "这里是一个可供监测的公开页面正文，包含机构的具体说明内容。"
    )
    response = FetchResponse(
        url,
        url,
        200,
        {"content-type": "text/html; charset=utf-8"},
        f"""
        <html><head><title>{title}</title></head><body>
          <main class="content"><h1>{title}</h1>
            <p>{body_text}</p>
          </main>
        </body></html>
        """.encode(),
        "text/html",
        "http",
        1,
    )

    result = HtmlInterpreter().interpret(
        response,
        profile=None,
        policy=UrlPolicy(Scope.for_seeds(["https://example.test/"])),
    )

    assert result.page_type is expected
