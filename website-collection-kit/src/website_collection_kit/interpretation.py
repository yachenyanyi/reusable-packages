"""Interpret fetched HTML into stable page facts and discovery signals."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from .contracts import Attachment, FieldHints, PageType, SiteProfile
from .ports import FetchResponse
from .url_policy import UrlPolicy

_SKIP_TEXT_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "canvas"}
_LAYOUT_SKIP_TAGS = {"nav", "header", "footer", "aside", "form"}
_LAYOUT_NAME_MARKERS = (
    "header",
    "footer",
    "nav",
    "sitenav",
    "mainnav",
    "topnav",
    "headnav",
    "footnav",
    "navbar",
    "navigation",
    "navcontent",
    "menubar",
    "menucontent",
    "sitemenu",
    "topbar",
    "toolbar",
    "sidebar",
    "breadcrumb",
    "crumb",
    "menu",
    "copyright",
    "businesscontent",
    "pagenav",
    "sitehead",
    "sitefooter",
    "sharebar",
    "loginbar",
    "页头",
    "页脚",
    "导航",
    "面包屑",
    "顶部",
    "底部",
    "菜单",
    "版权",
)
_CONTENT_NAME_MARKERS = (
    "content",
    "article",
    "detail",
    "news",
    "newstext",
    "richtext",
    "main",
    "正文",
    "内容",
)
_LOGIN_PATH_MARKERS = ("login", "signin", "sign-in", "auth")
_SEARCH_PATH_MARKERS = ("search", "query", "检索", "搜索")
_SCRIPT_NON_PAGE_EXTENSIONS = (
    ".css",
    ".js",
    ".map",
    ".mjs",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".ico",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
)
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_URL_ATTRIBUTES = ("data-url", "data-href", "data-link", "data-target", "data-redirect")
_NAVIGATION_REL_TOKENS = frozenset(("next", "prev", "first", "last", "up"))
_EMBEDDED_URL_ATTRIBUTES = {
    "audio": ("src",),
    "embed": ("src",),
    "frame": ("src",),
    "iframe": ("src",),
    "object": ("data",),
    "source": ("src",),
    "video": ("src",),
}
_ONCLICK_URL_RE = re.compile(
    r"(?:location(?:\.(?:href|assign|replace))?|window\.(?:open|location(?:\.(?:href|assign|replace))?)|document\.location(?:\.(?:href|assign|replace))?|open)\s*(?:\(\s*|=\s*)['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_GENERIC_URL_RE = re.compile(
    r"['\"]((?:(?:https?:)?//|/|\./|\.\./|\?)[^'\"]+)['\"]"
)
_DETAIL_HANDLER_RE = re.compile(
    r"\b(?P<handler>showDetail|showBaseDetail)\s*\(\s*"
    r"['\"](?P<item>[^'\"]+)['\"]\s*,\s*"
    r"['\"](?P<external>[^'\"]*)['\"]"
    r"(?P<tail>(?:\s*,\s*[^)]*)?)\)",
    re.IGNORECASE,
)
_META_REFRESH_URL_RE = re.compile(r"(?:^|;)\s*url\s*=\s*(.+)$", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*(?:月|[-/.])\s*(?P<day>\d{1,2})"
    r"(?:日)?(?:\s*[T ]\s*(?P<hour>\d{1,2})(?::|时)(?P<minute>\d{1,2})(?::(?P<second>\d{1,2}))?)?"
)


class _Node:
    __slots__ = ("attrs", "children", "data", "parent", "tag")

    def __init__(
        self, tag: str, attrs: dict[str, str], parent: _Node | None = None
    ) -> None:
        self.tag = tag.lower()
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent = parent
        self.data: list[str] = []

    def descendants(self) -> Iterable[_Node]:
        for child in self.children:
            yield child
            yield from child.descendants()


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(
            tag, {key.lower(): value or "" for key, value in attrs}, self._stack[-1]
        )
        self._stack[-1].children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(
            tag, {key.lower(): value or "" for key, value in attrs}, self._stack[-1]
        )
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].data.append(data)


@dataclass(frozen=True, slots=True)
class LinkObservation:
    raw_url: str
    source: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class InterpretedPage:
    title: str
    summary: str
    body: str
    section: str
    published_at: datetime | None
    published_at_confidence: float
    modified_at: datetime | None
    page_type: PageType
    classification_confidence: float
    classification_evidence: tuple[str, ...]
    language: str
    canonical_url: str
    links: tuple[LinkObservation, ...]
    attachments: tuple[Attachment, ...]


class HtmlInterpreter:
    """A dependency-light, conservative interpreter for public HTML pages."""

    def interpret(
        self,
        response: FetchResponse,
        *,
        profile: SiteProfile | None,
        policy: UrlPolicy,
    ) -> InterpretedPage:
        text = _decode_body(
            response.body, _header_value(response.headers, "content-type")
        )
        parser = _TreeParser()
        parser.feed(text)
        root = parser.root
        hints = profile.field_hints if profile else FieldHints()

        base_url = _document_base(root, response.final_url)
        canonical = self._canonical(root, response.final_url, policy, base_url)
        title = self._title(root, hints)
        body = self._body(root, hints)
        summary = self._summary(root, body)
        section = self._section(root, hints)
        published_at, published_confidence, modified_at = self._dates(root, text, hints)
        # Preserve the historical raw-link form when the document uses the
        # response URL as its implicit base.  The core already resolves those
        # links.  Only an explicit, different ``<base>`` needs to be applied
        # here so relative links are not resolved twice.
        link_base = base_url if base_url != response.final_url else None
        links = self._links(root, response.fetch_method, link_base)
        attachments = self._attachments(links, profile)
        page_type, confidence, evidence = self._classify(
            response.final_url,
            title,
            body,
            root,
            published_at,
            profile,
            response.media_type,
        )
        if urlsplit(response.final_url).path in {"", "/"}:
            # A home page commonly contains the dates of its latest items or
            # a site-wide update date.  Neither is the publication time of the
            # home page itself.
            published_at, published_confidence = None, 0.0
        return InterpretedPage(
            title=title,
            summary=summary,
            body=body,
            section=section,
            published_at=published_at,
            published_at_confidence=published_confidence,
            modified_at=modified_at,
            page_type=page_type,
            classification_confidence=confidence,
            classification_evidence=evidence,
            language=_language(body or title),
            canonical_url=canonical,
            links=links,
            attachments=attachments,
        )

    def _canonical(
        self,
        root: _Node,
        final_url: str,
        policy: UrlPolicy,
        base_url: str | None = None,
    ) -> str:
        for node in root.descendants():
            if (
                node.tag == "link"
                and "canonical" in node.attrs.get("rel", "").lower().split()
            ):
                candidate = policy.canonicalize(
                    node.attrs.get("href", ""), base_url or final_url
                )
                if candidate:
                    return candidate
        return policy.canonicalize(final_url)

    def _title(self, root: _Node, hints: FieldHints) -> str:
        for selector in hints.title_selectors:
            node = _first_selector(root, selector)
            if node:
                value = _clean_text(_node_text(node))
                if value:
                    return value[:500]
        heading = _heading_title(root)
        if heading:
            return heading[:500]
        for node in root.descendants():
            if node.tag == "title":
                value = _clean_text(_node_text(node))
                if value:
                    return value[:500]
        for node in root.descendants():
            if node.tag == "meta" and node.attrs.get("property", "").lower() in {
                "og:title",
                "twitter:title",
            }:
                value = _clean_text(node.attrs.get("content", ""))
                if value:
                    return value[:500]
        return ""

    def _summary(self, root: _Node, body: str) -> str:
        for node in root.descendants():
            key = (node.attrs.get("name", "") or node.attrs.get("property", "")).lower()
            if node.tag == "meta" and key in {"description", "og:description"}:
                value = _clean_text(node.attrs.get("content", ""))
                if value:
                    return value[:500]
        return body[:240]

    def _body(self, root: _Node, hints: FieldHints) -> str:
        candidates: list[_Node] = []
        for selector in hints.body_selectors:
            node = _first_selector(root, selector)
            if node:
                candidates.append(node)
        explicit_candidate = bool(candidates)
        if not candidates:
            candidates.extend(_content_nodes(root))
        if not candidates:
            candidates = [root]
        best = max(candidates, key=lambda node: len(_node_text(node, skip_layout=True)))
        value = _clean_text(_node_text(best, skip_layout=True))
        if len(value) < 80 and best is not root and not explicit_candidate:
            value = _clean_text(_node_text(root, skip_layout=True))
        return value[:2_000_000]

    def _section(self, root: _Node, hints: FieldHints) -> str:
        for selector in hints.section_selectors:
            node = _first_selector(root, selector)
            if node:
                value = _clean_text(_node_text(node))
                if value:
                    return value[:200]
        for node in root.descendants():
            name = _semantic_name(node)
            if any(
                token in name for token in ("breadcrumb", "crumb", "面包屑", "当前位置")
            ):
                value = _clean_text(_node_text(node))
                if value:
                    return value[:200]
        return ""

    def _dates(
        self,
        root: _Node,
        raw_html: str,
        hints: FieldHints,
    ) -> tuple[datetime | None, float, datetime | None]:
        published: tuple[datetime, float] | None = None
        modified: datetime | None = None
        for selector in hints.published_selectors:
            node = _first_selector(root, selector)
            if node:
                date = _parse_date(node.attrs.get("datetime", "") or _node_text(node))
                if date:
                    published = (date, 0.9)
                    break
        for node in root.descendants():
            if node.tag == "time" and published is None:
                date = _parse_date(node.attrs.get("datetime", "") or _node_text(node))
                if date:
                    published = (date, 0.8)
            if node.tag == "meta":
                key = (
                    node.attrs.get("property", "") or node.attrs.get("name", "")
                ).lower()
                date = _parse_date(node.attrs.get("content", ""))
                if (
                    date
                    and key
                    in {
                        "article:published_time",
                        "datepublished",
                        "publishdate",
                        "pubdate",
                    }
                    and published is None
                ):
                    published = (date, 0.95)
                if date and key in {
                    "article:modified_time",
                    "datemodified",
                    "lastmod",
                    "modified_time",
                }:
                    modified = date
        if published is None:
            json_date = _json_ld_date(root)
            if json_date:
                published = (json_date, 0.85)
        if published is None:
            fallback_text = _best_content_text(root)
            if not fallback_text:
                fallback_text = _clean_text(raw_html)
            for match in _DATE_RE.finditer(fallback_text):
                date = _parse_date(match.group(0))
                if date:
                    published = (date, 0.55)
                    break
        return (
            (published[0], published[1], modified)
            if published
            else (None, 0.0, modified)
        )

    def _links(
        self, root: _Node, fetch_method: str, base_url: str | None = None
    ) -> tuple[LinkObservation, ...]:
        default_source = "dom_link" if "playwright" in fetch_method else "html_link"
        links: list[LinkObservation] = []
        seen: set[tuple[str, str]] = set()
        for node in root.descendants():
            if node.tag == "link" and node.attrs.get("href"):
                rel = node.attrs.get("rel", "").lower()
                rel_tokens = set(rel.split())
                media_type = node.attrs.get("type", "").lower()
                if "sitemap" in rel or "sitemap" in media_type:
                    self._append_link(
                        links, seen, node.attrs["href"], "sitemap", "", base_url
                    )
                elif "alternate" in rel and any(
                    token in media_type for token in ("rss", "atom", "feed")
                ):
                    self._append_link(
                        links, seen, node.attrs["href"], "rss", "", base_url
                    )
                elif rel_tokens & _NAVIGATION_REL_TOKENS:
                    self._append_link(
                        links, seen, node.attrs["href"], default_source, "", base_url
                    )
                elif "alternate" in rel_tokens and node.attrs.get("hreflang"):
                    # Language variants are ordinary public pages.  Scope
                    # filtering later decides whether the variant belongs to
                    # this site; discovering it here avoids losing a host's
                    # alternate content tree.
                    self._append_link(
                        links, seen, node.attrs["href"], default_source, "", base_url
                    )
            if node.tag in {"a", "area"} and node.attrs.get("href"):
                self._append_link(
                    links,
                    seen,
                    node.attrs["href"],
                    default_source,
                    _node_text(node),
                    base_url,
                )
            for attr in _URL_ATTRIBUTES:
                if node.attrs.get(attr):
                    self._append_link(
                        links,
                        seen,
                        node.attrs[attr],
                        "data_url",
                        _node_text(node),
                        base_url,
                    )
            for attr in _EMBEDDED_URL_ATTRIBUTES.get(node.tag, ()):
                if node.attrs.get(attr):
                    self._append_link(
                        links,
                        seen,
                        node.attrs[attr],
                        "embedded_url",
                        _node_text(node),
                        base_url,
                    )
            if node.tag == "meta" and node.attrs.get("http-equiv", "").lower() == "refresh":
                match = _META_REFRESH_URL_RE.search(node.attrs.get("content", ""))
                if match:
                    self._append_link(
                        links,
                        seen,
                        match.group(1).strip().strip("'\""),
                        "meta_refresh",
                        "",
                        base_url,
                    )
            onclick = node.attrs.get("onclick", "")
            if onclick:
                matches = list(_ONCLICK_URL_RE.findall(onclick))
                # Do not stop after the first recognised call.  Legacy pages
                # commonly chain several navigations in one handler, and the
                # generic quoted-URL pass also catches direct assignments.
                matches.extend(_GENERIC_URL_RE.findall(onclick))
                for value in dict.fromkeys(matches):
                    self._append_link(
                        links,
                        seen,
                        value,
                        "onclick",
                        _node_text(node),
                        base_url,
                    )
                for value in _detail_handler_urls(onclick, root):
                    self._append_link(
                        links,
                        seen,
                        value,
                        "onclick",
                        _node_text(node),
                        base_url,
                    )
            if node.tag == "script":
                script = _node_text(node).replace("\\/", "/").replace('\\"', '"')
                for value in _GENERIC_URL_RE.findall(script):
                    value = _decode_script_url(value)
                    if _looks_like_script_page_reference(value):
                        self._append_link(
                            links, seen, value, "script_url", "", base_url
                        )
        return tuple(links)

    @staticmethod
    def _append_link(
        links: list[LinkObservation],
        seen: set[tuple[str, str]],
        raw_url: str,
        source: str,
        text: str,
        base_url: str | None = None,
    ) -> None:
        raw_url = raw_url.strip()
        if not raw_url or raw_url.lower().startswith(
            ("javascript:", "mailto:", "tel:", "#")
        ):
            return
        if raw_url.lower() in {"_self", "_blank", "_parent", "_top"}:
            # Common data-target values identify a browsing context, not a
            # resource URL (for example data-target="_self").
            return
        if base_url:
            try:
                raw_url = urljoin(base_url, raw_url)
            except ValueError:
                return
        key = (raw_url, source)
        if key not in seen:
            seen.add(key)
            links.append(
                LinkObservation(
                    raw_url=raw_url, source=source, text=_clean_text(text)[:200]
                )
            )


    def _attachments(
        self, links: Iterable[LinkObservation], profile: SiteProfile | None
    ) -> tuple[Attachment, ...]:
        extensions = (
            profile.attachment_extensions
            if profile
            else (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")
        )
        result: list[Attachment] = []
        seen: set[str] = set()
        for link in links:
            path = urlsplit(link.raw_url).path.lower()
            if any(path.endswith(extension) for extension in extensions):
                url = link.raw_url
                if url not in seen:
                    seen.add(url)
                    name = link.text or unquote(path.rsplit("/", 1)[-1])
                    result.append(Attachment(url=url, name=name[:300]))
        return tuple(result)

    def _classify(
        self,
        url: str,
        title: str,
        body: str,
        root: _Node,
        published_at: datetime | None,
        profile: SiteProfile | None,
        media_type: str,
    ) -> tuple[PageType, float, tuple[str, ...]]:
        parsed_url = urlsplit(url)
        path = parsed_url.path.lower()
        raw_query = parsed_url.query
        query = raw_query.lower()
        query_route = _query_route_context(raw_query)
        route_path = f"{path}/{query_route}" if query_route else path
        for route in sorted(
            profile.route_patterns if profile else (),
            key=lambda item: len(item.pattern),
            reverse=True,
        ):
            if re.search(route.pattern, url, re.IGNORECASE):
                evidence = (f"route_pattern:{route.label or route.pattern}",)
                return route.page_type, 0.95, evidence
        if media_type and not media_type.lower().startswith(
            ("text/html", "application/xhtml")
        ):
            return PageType.ATTACHMENT, 0.98, (f"media_type:{media_type}",)
        if path in {"", "/"} and not query_route:
            return PageType.HOME, 0.99, ("root_path",)

        # URL shape and page-specific structure are stronger evidence than
        # arbitrary words in the extracted body.  Institutional sites often
        # repeat "联系我们" and "登录" in every header/footer, which used to
        # make a news detail page look like a contact or login page.
        list_like = _looks_like_list(root)
        explicit_detail = _has_explicit_detail_path(path)
        dated_detail = _has_dated_detail_path(path)
        numeric_detail = _has_numeric_detail_path(path)
        compound_detail = _has_compound_detail_path(path)
        static_page_detail = _has_numbered_static_page_path(path) and len(body) >= 180
        query_static_detail = _has_query_static_page_path(query_route)
        query_detail = _has_query_detail_path(path, query)
        has_notice = _path_has_any(route_path, "notice", "announcement", "inform", "tzgg") or _title_has_any(
            title, "notice", "notices", "announcement", "announcements", "通知", "公告", "公示"
        )
        has_news = _path_has_any(
            route_path,
            "news",
            "newsstatus",
            "media",
            "conference",
            "dynamic",
            "act",
            "activity",
            "activities",
            "event",
            "events",
            "xw",
            "djgh",
        )
        has_product = (
            _path_has_any(
                route_path,
                "product",
                "products",
                "jjfa",
                "solution",
                "case",
                "cases",
                "apply",
                "application",
                "equipment",
                "intelligentequipment",
                "smartmine",
                "smartocean",
                "railwayequipment",
                "algorithmcenter",
                "digital",
                "internethospital",
                "brainai",
                "ctimageai",
                "industry",
                "achievement",
                "achievements",
                "service",
                "services",
                "system",
                "custom",
                "project",
            )
            or (compound_detail and _path_has_any(path, "apply", "application", "case", "cases", "service"))
            or _title_has_any(
                title,
                "product",
                "products",
                "产品",
                "解决方案",
                "应用方案",
                "智能装备",
                "产品服务",
                "行业应用",
                "落地案例",
                "企划咨询",
                "成为合作伙伴",
                "数据增值服务平台",
            )
            or _body_has_any(
                body,
                "技术成果类别",
                "成果类别",
                "应用技术成果",
                "研究成果简介",
                "产品简介",
            )
        )
        has_profile = _path_has_any(
            route_path,
            "about",
            "aboutinfo",
            "profile",
            "brief-introduction",
            "company",
            "culture",
            "honor",
            "history",
            "organization",
            "framework",
            "incubator",
            "overview",
            "vision",
            "culture",
            "cooperation",
            "collaboration",
            "alliance",
            "partner",
            "partners",
            "management",
            "team",
            "member",
            "introduction",
            "datesources",
        ) or _title_has_any(
            title,
            "about",
            "关于我们",
            "机构简介",
            "院所简介",
            "机构介绍",
            "院所介绍",
            "公司介绍",
            "研究院概况",
            "机构概况",
            "公司简介",
            "企业文化",
            "公司荣誉",
            "荣誉资质",
            "发展历程",
            "组织架构",
            "专家团队",
            "研发团队",
            "技术团队",
            "团队介绍",
            "技术专家",
            "孵化空间",
            "人才理念",
            "人才培养",
            "管理团队",
            "成员单位",
            "联创中心",
            "材料大数据平台",
            "合作高校",
            "创新联盟",
            "研究合作",
            "Company Profile",
            "Academic Exchange",
            "Business cooperation",
            "Series of Activities",
            "Notices and Announcements",
            "News Center",
            "核心价值观",
            "企业价值观",
            "使命愿景",
            "简介",
            "概况",
        )
        has_research = _path_has_any(
            route_path,
            "research",
            "strategic-research",
            "direction",
            "scientific",
            "yanfa",
            "platform",
            "achievement",
            "achievements",
            "result",
            "results",
            "outcome",
            "outcomes",
            "source",
            "sources",
            "technology",
            "technologies",
            "innovation",
            "project",
            "xmzj",
            "datesources",
        ) or _title_has_any(
            title,
            "研究方向",
            "研发方向",
            "研究成果",
            "科研成果",
            "技术成果",
            "成果转化",
            "工程转化",
            "研发平台",
            "研发中心",
            "研究中心",
            "技术中心",
            "技术服务",
            "共性技术事业部",
            "产业孵化",
            "高光谱",
            "超光谱",
            "技术知识",
            "技术原理",
        ) or (_path_has_any(route_path, "resource") and not list_like)
        has_recruitment = _path_has_any(
            route_path, "recruit", "recruitment", "career", "job", "rczp"
        ) or _title_has_any(
            title, "recruit", "recruitment", "career", "招聘", "人才招聘", "招聘信息", "岗位"
        )
        has_contact = _path_has_any(route_path, "contact", "connect", "lxwm", "message", "feedback", "joinus", "join") or _title_has_any(
            title, "contact", "connect", "联系方式", "联系我们", "联系"
        )
        # A detail page can contain many related links.  Link density alone
        # therefore must not defeat a strong, page-specific content signal.
        generic_detail = _has_detail_metadata(body, published_at) and not any(
            (has_notice, has_news, has_product, has_profile, has_research, has_recruitment, has_contact)
        )
        detail_route = explicit_detail or dated_detail or compound_detail or static_page_detail or query_static_detail or generic_detail or (
            numeric_detail
            and (
                not list_like
                or _has_singular_news_detail_path(path)
                or _has_nested_news_detail_path(path)
                or _has_product_detail_path(path)
                or _has_detail_metadata(body, published_at)
            )
        ) or query_detail

        if _looks_like_login_page(path, title, root):
            return PageType.LOGIN, 0.94, ("login_route_or_form",)
        if _path_has_any(path, *_SEARCH_PATH_MARKERS) or _title_has_any(
            title, *_SEARCH_PATH_MARKERS
        ):
            return PageType.SEARCH, 0.9, ("search_route_or_title",)

        if detail_route:
            if has_notice or _title_has_any(
                title,
                "notice",
                "notices",
                "announcement",
                "announcements",
                "通知",
                "公告",
                "公示",
            ):
                return PageType.NOTICE, 0.9, ("detail_route", "notice_route")
            if has_product:
                return PageType.PRODUCT, 0.88, ("detail_route", "product_route")
            if has_profile:
                return PageType.PROFILE, 0.88, ("detail_route", "profile_route")
            if has_research:
                return PageType.RESEARCH, 0.88, ("detail_route", "research_route")
            if has_recruitment:
                return PageType.RECRUITMENT, 0.88, ("detail_route", "recruitment_route")
            return PageType.ARTICLE, 0.8, ("detail_route",)

        path_list = _is_list_path(path) or _is_query_list_path(query_route)
        if has_notice:
            if list_like or path_list:
                return PageType.NOTICE_LIST, 0.86, ("notice_route", "list_links")
            return PageType.NOTICE, 0.78, ("notice_route",)
        if has_news:
            if list_like or path_list:
                return PageType.NEWS_LIST, 0.86, ("news_route", "list_links")
            if published_at and len(body) >= 120:
                return PageType.ARTICLE, 0.76, ("news_route", "published_date", "substantial_body")
            return PageType.ARTICLE, 0.68, ("news_route",)
        if has_product:
            return PageType.PRODUCT, 0.82, ("product_route",)
        if has_profile:
            return PageType.PROFILE, 0.82, ("profile_route",)
        if has_research:
            return PageType.RESEARCH, 0.82, ("research_route",)
        if has_recruitment:
            return PageType.RECRUITMENT, 0.82, ("recruitment_route",)
        if has_contact:
            return PageType.CONTACT, 0.9, ("contact_route",)
        if list_like or path_list:
            return PageType.LIST, 0.72, ("link_density",)
        if published_at and len(body) >= 120:
            return PageType.ARTICLE, 0.76, ("published_date", "substantial_body")

        # Only use short, page-specific titles as a final semantic hint.  The
        # body is deliberately excluded here because global navigation is
        # frequently repeated outside semantic <nav>/<header>/<footer> tags.
        if _title_has_any(title, "notice", "announcement", "通知", "公告", "公示"):
            return PageType.NOTICE, 0.7, ("title_keyword:notice",)
        if _title_has_any(title, "news", "动态", "资讯"):
            return PageType.ARTICLE, 0.65, ("title_keyword:news",)
        return PageType.OTHER, 0.35, ("no_specific_signal",)


def _detail_handler_urls(onclick: str, root: _Node) -> tuple[str, ...]:
    """Recover internal detail URLs hidden behind common CMS click handlers.

    Several public CMS templates render content cards as ``div`` elements and
    keep only an item id in ``showDetail``/``showBaseDetail`` calls.  The
    browser resolves those ids using the page's navigation table, so treating
    the handler as an ordinary quoted-URL source loses a real detail page.
    This deliberately handles only the read-only detail-handler conventions;
    arbitrary JavaScript is not executed or interpreted as navigation.
    """

    route_paths = _script_route_paths(root)
    result: list[str] = []
    for match in _DETAIL_HANDLER_RE.finditer(onclick):
        external = _decode_script_url(match.group("external")).strip()
        if external:
            continue
        item = _decode_script_url(match.group("item")).strip()
        if not item:
            continue
        handler = match.group("handler").lower()
        if handler == "showbasedetail":
            tail = match.group("tail")
            is_list = bool(
                re.search(r",\s*[^,]+\s*,\s*true\s*$", tail, re.IGNORECASE)
            )
            result.append(("?" if is_list else "?id=") + item)
            continue
        tail = match.group("tail")
        column_ids = re.findall(r"['\"]([^'\"]+)['\"]", tail)
        for column_id in column_ids:
            path = route_paths.get(column_id)
            if path:
                result.append(path + ("&" if "?" in path else "?") + "id=" + item)
                break
    return tuple(dict.fromkeys(result))


def _script_route_paths(root: _Node) -> dict[str, str]:
    """Extract the small id-to-path table used by read-only CMS handlers."""

    script_text = "\n".join(
        _node_text(node) for node in root.descendants() if node.tag == "script"
    )
    paths: dict[str, str] = {}
    pair = re.compile(
        r"(?:[\"']id[\"']|\bid)\s*:\s*[\"'](?P<id>[^\"']+)[\"']"
        r"(?:(?![\"']id[\"']|\bid\s*:).){0,500}?"
        r"(?:[\"']path[\"']|\bpath)\s*:\s*[\"'](?P<path>[^\"']*)[\"']",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pair.finditer(script_text):
        identifier = _decode_script_url(match.group("id")).strip()
        path = _decode_script_url(match.group("path")).strip()
        if identifier and path.startswith(("/", "?")):
            paths.setdefault(identifier, path)
    return paths


def _decode_body(body: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    if not charset:
        prefix = body[:4096].decode("ascii", errors="ignore")
        match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", prefix, re.IGNORECASE)
        if match:
            charset = match.group(1)
    encodings = (
        [charset, "utf-8", "gb18030", "cp1252"]
        if charset
        else ["utf-8", "gb18030", "cp1252"]
    )
    best = ""
    best_replacements = None
    for encoding in encodings:
        if not encoding:
            continue
        try:
            decoded = body.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            continue
        # A few browser-oriented CMS endpoints append a large NUL-filled
        # buffer after otherwise valid HTML.  Keeping it would make the
        # parsed body enormous and hide the real page content.  NUL is not a
        # meaningful HTML character, so trim a terminal run and neutralise
        # any remaining embedded bytes before parsing.
        decoded = decoded.rstrip("\x00").replace("\x00", " ")
        replacements = decoded.count("\ufffd")
        if best_replacements is None or replacements < best_replacements:
            best, best_replacements = decoded, replacements
    return best


def _header_value(headers, name: str) -> str:
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return ""


def _node_text(node: _Node, *, skip_layout: bool = False) -> str:
    pieces = list(node.data)
    for child in node.children:
        if child.tag in _SKIP_TEXT_TAGS or _is_hidden_node(child) or (
            skip_layout and _is_layout_node(child)
        ):
            continue
        pieces.append(_node_text(child, skip_layout=skip_layout))
    return " ".join(pieces)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def _document_base(root: _Node, final_url: str) -> str:
    """Return the first usable HTML ``base`` URL, matching browser behavior."""

    for node in root.descendants():
        if node.tag != "base" or not node.attrs.get("href"):
            continue
        try:
            candidate = urljoin(final_url, node.attrs["href"].strip())
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            return candidate
    return final_url


def _is_hidden_node(node: _Node) -> bool:
    style = re.sub(r"\s+", "", node.attrs.get("style", "").lower())
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if node.attrs.get("aria-hidden", "").lower() == "true":
        return True
    compact = _compact_attribute_value(node)
    return any(marker in compact for marker in ("dnone", "hidden", "invisible"))


def _compact_attribute_value(node: _Node) -> str:
    value = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}".lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value)


def _is_layout_node(node: _Node) -> bool:
    return node.tag in _LAYOUT_SKIP_TAGS or any(
        marker in _compact_attribute_value(node) for marker in _LAYOUT_NAME_MARKERS
    )


def _inside_layout(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        if _is_layout_node(current):
            return True
        current = current.parent
    return False


def _is_content_node(node: _Node) -> bool:
    if _is_layout_node(node) or _is_hidden_node(node):
        return False
    if node.tag in {"main", "article", "section"}:
        return True
    name = _compact_attribute_value(node)
    return any(marker in name for marker in _CONTENT_NAME_MARKERS)


def _content_nodes(root: _Node) -> list[_Node]:
    return [node for node in root.descendants() if _is_content_node(node)]


def _best_content_text(root: _Node) -> str:
    candidates = _content_nodes(root)
    if not candidates:
        return _clean_text(_node_text(root, skip_layout=True))
    return max(
        (_clean_text(_node_text(node, skip_layout=True)) for node in candidates),
        key=len,
        default="",
    )


def _heading_title(root: _Node) -> str:
    best: tuple[int, str] | None = None
    for node in root.descendants():
        if node.tag not in {"h1", "h2", "h3"} or _inside_layout(node) or _is_hidden_node(node):
            continue
        value = _clean_text(_node_text(node, skip_layout=True))
        if not value or len(value) > 500:
            continue
        ancestors: list[_Node] = []
        current = node.parent
        while current is not None:
            ancestors.append(current)
            current = current.parent
        names = " ".join(_compact_attribute_value(item) for item in (node, *ancestors))
        score = {"h1": 30, "h2": 20, "h3": 10}[node.tag]
        if any(marker in names for marker in ("title", "headline", "subject", "subtitle", "artititle", "newstitle")):
            score += 25
        if any(_is_content_node(item) for item in ancestors):
            score += 15
        if value in {"首页", "主页", "返回", "更多", "more", "home"}:
            score -= 30
        if len(value) > 160:
            score -= 10
        candidate = (score, value)
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else ""


def _path_compact(path: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", path.lower())


def _query_route_context(query: str) -> str:
    """Expose path-like query keys as semantic route segments.

    Some CMSs encode routes as blank-valued query keys, for example
    ``?News%2F241.html=``.  Treating the decoded key as route context lets the
    normal classifier handle those pages without giving query parameters a
    second, site-specific classification system.
    """

    segments: list[str] = []
    for key, _ in parse_qsl(query, keep_blank_values=True):
        if not key:
            continue
        key = re.sub(r"(?<=[a-z])(?=[A-Z])", "/", unquote(key))
        segments.append(key)
    return "/".join(segments)


def _path_has_any(path: str, *markers: str) -> bool:
    segments = []
    for raw_segment in re.split(r"/", path):
        if not raw_segment:
            continue
        raw_segment = re.sub(
            r"\.(?:html?|php|asp|aspx|jsp)$", "", raw_segment.lower()
        )
        compact = re.sub(r"[^a-z0-9\u3400-\u9fff]", "", raw_segment)
        segments.append(compact)
        segments.extend(
            re.sub(r"[^a-z0-9\u3400-\u9fff]", "", token)
            for token in re.split(r"[-_]+", raw_segment)
            if token
        )
    normalised = [
        re.sub(r"[^a-z0-9\u3400-\u9fff]", "", marker.lower())
        for marker in markers
    ]
    return any(
        marker and any(segment == marker or segment.startswith(marker) for segment in segments)
        for marker in normalised
    )


def _title_has_any(title: str, *markers: str) -> bool:
    value = title.strip().lower()
    if not value or len(value) > 120:
        return False
    return any(_keyword_present(value, marker) for marker in markers)


def _body_has_any(body: str, *markers: str) -> bool:
    value = body[:4_000].lower()
    return bool(value) and any(marker.lower() in value for marker in markers)


def _decode_script_url(value: str) -> str:
    """Decode URL escapes commonly used in JSON-like script literals."""

    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    value = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        value,
    )
    return value.replace("\\/", "/").replace("\\&", "&")


def _has_explicit_detail_path(path: str) -> bool:
    return bool(
        re.search(r"/(?:detail|article|show|view)(?:/|$)", path, re.IGNORECASE)
        or re.search(r"/about[_-]?info(?:/|$)", path, re.IGNORECASE)
        or re.search(r"/archives?/\d+(?:\.[^/]*)?$", path, re.IGNORECASE)
        or re.search(r"/article-item-\d+(?:\.[^/]*)?$", path, re.IGNORECASE)
        or re.search(r"/page-\d+-\d+(?:\.[^/]*)?$", path, re.IGNORECASE)
        or re.search(r"/post/\d+(?:\.[^/]*)?$", path, re.IGNORECASE)
    )


def _has_dated_detail_path(path: str) -> bool:
    return bool(
        re.search(
            r"/newsstatus/(?:news|inform)/20\d{2}[-/]\d{1,2}[-/]\d{1,2}/\d+",
            path,
            re.IGNORECASE,
        )
        or re.search(
            r"/20\d{2}/\d{2,4}/[^/]+/page\.html?$",
            path,
            re.IGNORECASE,
        )
        or re.search(
            r"/[^/]+/info/20\d{2}/\d+\.html?$",
            path,
            re.IGNORECASE,
        )
    )


def _has_numeric_detail_path(path: str) -> bool:
    return bool(
        re.search(
            r"/(?:news|notice|new|product|products|jjfa)(?:/[^/]+)*/[^/]*\d[^/]*(?:\.(?:html?|htm))?$",
            path,
            re.IGNORECASE,
        )
    )


def _has_compound_detail_path(path: str) -> bool:
    """Recognise common application/case detail IDs such as ``apply/7_10``."""

    return bool(
        re.search(
            r"/(?:apply|application|case|cases|service|solution)/[^/]*\d[^/]*[_-][^/]*$",
            path,
            re.IGNORECASE,
        )
    )


def _has_numbered_static_page_path(path: str) -> bool:
    """Recognise CMS static pages exposed as root-level ``page35`` routes."""

    return bool(re.search(r"^/page\d+(?:\.[^/]*)?$", path, re.IGNORECASE))


def _has_query_detail_path(path: str, query: str) -> bool:
    pairs = parse_qsl(query, keep_blank_values=True)
    identifier_keys = {
        "id",
        "itemid",
        "articleid",
        "newsid",
        "contentid",
        "documentid",
        "uuid",
        "did",
    }
    has_identifier = any(
        key.lower() in identifier_keys and value.strip()
        for key, value in pairs
    )
    has_key_only_identifier = any(
        not value.strip() and _looks_like_identifier_key(key)
        for key, value in pairs
    )
    if has_identifier or has_key_only_identifier:
        basename = path.rsplit("/", 1)[-1]
        if re.fullmatch(
            r"(?:news|notice|article)\.(?:asp|aspx|php|jsp)$",
            basename,
            re.IGNORECASE,
        ):
            return False
        # ``a=lists`` and equivalent forms identify a collection page even
        # when it also carries a category id.  Other identifier-bearing CMS
        # routes are presumed detail pages; the later semantic classifier
        # chooses article/product/profile/research.
        if any(
            key.lower() in {"a", "action", "act", "mode", "type"}
            and value.lower() in {"list", "lists", "index", "search"}
            for key, value in pairs
        ):
            return False
        return not _is_list_path(path) or _path_has_any(
            path,
            "achievement",
            "source",
            "sources",
            "product",
            "service",
            "about",
            "news",
            "notice",
            "article",
            "core",
            "fuwu",
            "ky-source",
        )
    if not any(key.lower() == "id" for key, _ in pairs):
        decoded_route = _query_route_context(query)
        return _has_query_route_detail(decoded_route)
    return bool(
        re.search(
            r"(?:^|&)(?:a|action|act|mode|type)=(?:detail|show|view)(?:&|$)",
            query,
            re.IGNORECASE,
        )
        or re.search(r"/(?:news|article)[_-]?views?\.asp$", path, re.IGNORECASE)
        or re.search(
            r"/(?:news|article|enterprise|product|research|info|content)[_-]views?\.asp$",
            path,
            re.IGNORECASE,
        )
        or re.search(r"/(?:detail|show|view)[^/]*\.asp$", path, re.IGNORECASE)
        or re.search(r"/(?:news|article|video|core)(?:\.html?)?$", path, re.IGNORECASE)
        or _has_query_route_detail(_query_route_context(query))
    )


def _has_query_route_detail(route: str) -> bool:
    return bool(
        re.search(
            r"(?:^|/)(?:news|notice|article|post)/[^/]*\d+(?:\.[^/]*)?$",
            route,
            re.IGNORECASE,
        )
    )


def _has_query_static_page_path(route: str) -> bool:
    return bool(
        re.search(r"(?:^|/)(?:page|pages)[_-]?\d+(?:/|$)", route, re.IGNORECASE)
    )


def _is_query_list_path(route: str) -> bool:
    return bool(
        re.search(
            r"(?:^|/)(?:en/)?(?:news|notice|article|job|recruitment|research)/?$",
            route,
            re.IGNORECASE,
        )
    )


def _has_singular_news_detail_path(path: str) -> bool:
    return bool(
        re.search(r"/new/[^/]*\d[^/]*\.(?:html?|htm)$", path, re.IGNORECASE)
    )


def _has_nested_news_detail_path(path: str) -> bool:
    return bool(
        re.search(
            r"/(?:news|notice|new)/[^/]+/[^/]*\d[^/]*\.(?:html?|htm)$",
            path,
            re.IGNORECASE,
        )
    )


def _has_product_detail_path(path: str) -> bool:
    return bool(
        re.search(
            r"/(?:product|products|jjfa)/[^/]*\d[^/]*\.(?:html?|htm)$",
            path,
            re.IGNORECASE,
        )
    )


def _has_detail_metadata(body: str, published_at: datetime | None) -> bool:
    if published_at is None or len(body) < 80:
        return False
    value = body[:2_000].lower()
    if any(
        marker in value
        for marker in (
            "发布时间",
            "发布日期",
            "发表时间",
            "来源：",
            "来源:",
            "作者：",
            "作者:",
            "publish time",
            "published",
            "view count",
        )
    ):
        return True
    # Some older sites expose only one bare publication date and surround the
    # article with related links.  A list page normally contains several
    # dates; one date plus a substantial body is useful detail evidence.
    return len(_DATE_RE.findall(value)) == 1 and len(body) >= 180


def _is_list_path(path: str) -> bool:
    return bool(
        re.search(
            r"/(?:list|index|act|activity|activities|event|events|news\d*|notice\d*|rczp\d*|xmzj\d*)(?:[-_]\d+)*(?:\.(?:html?|htm))?/?$",
            path,
            re.IGNORECASE,
        )
    )


def _looks_like_login_page(path: str, title: str, root: _Node) -> bool:
    if _path_has_any(path, *_LOGIN_PATH_MARKERS) or _title_has_any(
        title, "login", "signin", "登录", "用户登录"
    ):
        return True
    for node in root.descendants():
        if node.tag != "input" or node.attrs.get("type", "").lower() != "password":
            continue
        form = node.parent
        while form is not None and form.tag != "form":
            form = form.parent
        if form is not None:
            form_text = _clean_text(_node_text(form, skip_layout=True)).lower()
            if any(_keyword_present(form_text, marker) for marker in ("login", "signin", "登录")):
                return True
    return False


def _semantic_name(node: _Node) -> str:
    value = f"{node.attrs.get('id', '')} {node.attrs.get('class', '')}".lower()
    return re.sub(r"[^\w\u3400-\u9fff]", "", value)


def _first_selector(root: _Node, selector: str) -> _Node | None:
    selector = selector.strip()
    if not selector:
        return None
    for node in root.descendants():
        if _matches_selector(node, selector):
            return node
    return None


def _matches_selector(node: _Node, selector: str) -> bool:
    selector = selector.strip()
    if selector.startswith("#"):
        return node.attrs.get("id", "") == selector[1:]
    if selector.startswith("."):
        return selector[1:] in node.attrs.get("class", "").split()
    if "." in selector:
        tag, class_name = selector.split(".", 1)
        return (
            node.tag == tag.lower()
            and class_name in node.attrs.get("class", "").split()
        )
    if selector.startswith("[") and selector.endswith("]") and "=" in selector:
        key, value = selector[1:-1].split("=", 1)
        return node.attrs.get(key.strip()) == value.strip("\"'")
    return node.tag == selector.lower()


def _parse_date(value: str) -> datetime | None:
    value = _clean_text(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC)
    except ValueError:
        pass
    match = _DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour") or 0),
            int(match.group("minute") or 0),
            int(match.group("second") or 0),
            tzinfo=UTC,
        )
    except ValueError:
        return None


def _json_ld_date(root: _Node) -> datetime | None:
    for node in root.descendants():
        if node.tag != "script" or "ld+json" not in node.attrs.get("type", "").lower():
            continue
        raw = _node_text(node)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            continue
        pending = deque([data])
        while pending:
            item = pending.popleft()
            if isinstance(item, dict):
                for key in ("datePublished", "dateCreated"):
                    result = _parse_date(str(item.get(key, "")))
                    if result:
                        return result
                pending.extend(
                    child for child in item.values() if isinstance(child, (dict, list))
                )
            elif isinstance(item, list):
                pending.extend(item)
    return None


def _looks_like_list(root: _Node) -> bool:
    links = [
        node
        for node in root.descendants()
        if (
            node.tag == "a"
            and node.attrs.get("href")
            and not _inside_layout(node)
            and not _is_hidden_node(node)
        )
    ]
    if len(links) >= 6:
        return True
    list_nodes = [
        node
        for node in root.descendants()
        if node.tag in {"ul", "ol", "table"}
        and not _inside_layout(node)
        and not _is_hidden_node(node)
    ]
    return any(len(_node_text(node, skip_layout=True)) >= 120 for node in list_nodes)


def _language(value: str) -> str:
    if not value:
        return ""
    chinese = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if chinese and chinese >= latin * 0.05:
        return "zh"
    if latin:
        return "en"
    return ""


def _keyword_present(value: str, keyword: str) -> bool:
    if keyword.isascii() and keyword.replace("_", "").isalnum():
        return (
            re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", value, re.IGNORECASE)
            is not None
        )
    return keyword in value


def _looks_like_data_endpoint(value: str) -> bool:
    parsed = urlsplit(value)
    path = parsed.path.lower()
    route = path + ("?" + parsed.query.lower() if parsed.query else "")
    is_endpoint = any(
        token in route
        for token in ("/api", "/ajax", "/json", "?page", "?id", "list")
    ) or path.endswith(".json")
    if not is_endpoint:
        return False
    segments = {
        segment
        for segment in re.split(r"[/_.-]+", path)
        if segment
    }
    if segments & {
        "create",
        "delete",
        "destroy",
        "insert",
        "remove",
        "save",
        "submit",
        "update",
        "upload",
    }:
        return False
    return True


def _looks_like_script_page_reference(value: str) -> bool:
    """Keep quoted navigation hints while avoiding ordinary static assets."""

    parsed = urlsplit(value)
    path = parsed.path.lower()
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(
        key.lower() in {"id", "uuid", "itemid", "articleid", "newsid", "did"}
        and not item_value
        for key, item_value in pairs
    ):
        # Template scripts frequently contain placeholders such as ``?id=``;
        # they are not useful page identities.  Key-only UUID routes are kept
        # because they are a real convention used by several CMSs.
        return False
    if _looks_like_data_endpoint(value):
        return True
    if path.endswith(_SCRIPT_NON_PAGE_EXTENSIONS):
        return False
    if re.search(r"\.(?:html?|php|asp|aspx|jsp)(?:$|[?#])", path):
        return True
    if parsed.query:
        if any(
            key.lower() in {"id", "uuid", "itemid", "articleid", "newsid", "did"}
            and (value or _looks_like_identifier_key(key))
            for key, value in pairs
        ):
            return True
    return _path_has_any(
        path,
        "about",
        "article",
        "achievement",
        "case",
        "contact",
        "cooperation",
        "detail",
        "event",
        "news",
        "notice",
        "product",
        "profile",
        "research",
        "result",
        "service",
        "source",
        "team",
        "technology",
    )


def _looks_like_identifier_key(value: str) -> bool:
    compact = re.sub(r"[^0-9a-f-]", "", value.lower())
    return bool(
        re.search(r"\d{10,}", value)
        or re.fullmatch(r"[0-9a-f]{8,}(?:-[0-9a-f]{4,}){1,4}", compact)
    )
