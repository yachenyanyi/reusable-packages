from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from website_collection_kit import (
    AcquisitionError,
    Budget,
    CollectionIntent,
    CollectionSpec,
    CollectionStatus,
    FileEvidenceStore,
    HttpxAcquisition,
    HybridAcquisition,
    NoAcquisitionCapabilityError,
    PlaywrightAcquisition,
    RenderingRequirement,
    Scope,
    WebsiteCollectionKit,
)
from website_collection_kit.ports import EvidencePayload, FetchRequest, FetchResponse


class _LocalSite(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/robots.txt":
            body, status, media_type = (
                b"User-agent: *\nDisallow: /private\n",
                200,
                "text/plain",
            )
        elif self.path == "/":
            body, status, media_type = b"", 302, "text/plain"
        elif self.path == "/home":
            body, status, media_type = (
                b'<html><head><title>Home</title></head><body><main><a href="/article">Article</a><a href="/private">Private</a></main></body></html>',
                200,
                "text/html; charset=utf-8",
            )
        elif self.path == "/dynamic":
            body, status, media_type = (
                b'<html><body><main id="dynamic"></main><script>setTimeout(() => { document.getElementById("dynamic").textContent = "Loaded dynamically"; }, 50);</script></body></html>',
                200,
                "text/html; charset=utf-8",
            )
        elif self.path == "/article":
            body, status, media_type = (
                b"<html><head><title>Article</title></head><body><article><h1>Article</h1><p>Public article content.</p></article></body></html>",
                200,
                "text/html; charset=utf-8",
            )
        elif self.path == "/private":
            body, status, media_type = b"private", 200, "text/plain"
        else:
            body, status, media_type = b"not found", 404, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/":
            self.send_header("Location", "/home")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def local_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_httpx_adapter_performs_real_local_collection(local_site: str) -> None:
    acquisition = HttpxAcquisition(timeout_seconds=5)
    kit = WebsiteCollectionKit(acquisition)
    result = await kit.collect_site(
        CollectionSpec(
            "http-local",
            "local-site",
            CollectionIntent.SITE_SWEEP,
            Scope.for_seeds([local_site + "/"]),
            seeds=(local_site + "/",),
            budget=Budget(
                max_pages=10, max_candidates=20, max_depth=3, max_duration_seconds=10
            ),
        )
    )
    await kit.close()

    assert result.status is CollectionStatus.COMPLETED
    assert {page.final_url for page in result.pages} == {
        local_site + "/home",
        local_site + "/article",
    }
    assert any(item.reason == "robots_disallowed" for item in result.exclusions)


@pytest.mark.asyncio
async def test_playwright_adapter_renders_local_page(local_site: str) -> None:
    acquisition = PlaywrightAcquisition(timeout_seconds=10)
    try:
        response = await acquisition.fetch(FetchRequest(local_site + "/home"))
    except Exception as exc:
        await acquisition.close()
        message = str(exc).lower()
        if (
            "executable doesn't exist" in message
            or "requires the 'browser' extra" in message
            or "browser" in message
            and "installed" in message
        ):
            pytest.skip("Playwright browser is not installed in this environment")
        raise
    finally:
        await acquisition.close()

    assert response.fetch_method == "playwright"
    assert response.status == 200
    assert b"Article" in response.body


@pytest.mark.asyncio
async def test_playwright_adapter_waits_for_delayed_dom_content(
    local_site: str,
) -> None:
    acquisition = PlaywrightAcquisition(timeout_seconds=10, settle_timeout_seconds=1)
    try:
        response = await acquisition.fetch(FetchRequest(local_site + "/dynamic"))
    except Exception as exc:
        await acquisition.close()
        message = str(exc).lower()
        if (
            "executable doesn't exist" in message
            or "requires the 'browser' extra" in message
            or "browser" in message
            and "installed" in message
        ):
            pytest.skip("Playwright browser is not installed in this environment")
        raise
    finally:
        await acquisition.close()

    assert response.status == 200
    assert b"Loaded dynamically" in response.body


def test_file_evidence_store_is_content_addressed(tmp_path) -> None:
    store = FileEvidenceStore(tmp_path)
    reference = store.save(
        EvidencePayload(
            "c1",
            "https://example.test/",
            "html/raw",
            b"<html>",
            "text/html",
            {"cookie": "secret", "content-type": "text/html"},
        )
    )

    assert store.read(reference) == b"<html>"
    assert reference.ref.startswith("evidence://sha256/")
    assert not list(tmp_path.rglob("*secret*"))


def test_memory_evidence_store_uses_the_same_safe_reference_format() -> None:
    from website_collection_kit import MemoryEvidenceStore

    store = MemoryEvidenceStore()
    reference = store.save(
        EvidencePayload(
            "c1", "https://example.test/", "html/raw", b"<html>", "text/html", {}
        )
    )

    assert "/html-raw" in reference.ref
    assert store.read(reference) == b"<html>"


def test_file_evidence_store_handles_concurrent_same_content_writers(tmp_path) -> None:
    store = FileEvidenceStore(tmp_path)
    payload = EvidencePayload(
        "c1", "https://example.test/", "html/raw", b"<html>", "text/html", {}
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        references = list(executor.map(store.save, [payload] * 16))

    assert all(reference == references[0] for reference in references)
    assert store.read(references[0]) == b"<html>"
    assert not list(tmp_path.rglob("*.tmp"))


class _StaticShell:
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        return FetchResponse(
            request.url,
            request.url,
            200,
            {"content-type": "text/html"},
            b'<div id="app"><script>boot()</script></div>',
            "text/html",
            "http",
            1,
        )


class _RenderedPage:
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        return FetchResponse(
            request.url,
            request.url,
            200,
            {"content-type": "text/html"},
            b"<main>Rendered content</main>",
            "text/html",
            "playwright",
            1,
        )


@pytest.mark.asyncio
async def test_hybrid_acquisition_falls_back_for_app_shell() -> None:
    result = await HybridAcquisition(_StaticShell(), _RenderedPage()).fetch(
        FetchRequest("https://example.test/", RenderingRequirement.PREFERRED)
    )

    assert result.fetch_method == "playwright"
    assert b"Rendered content" in result.body


class _MissingRendered:
    async def fetch(self, request: FetchRequest) -> FetchResponse:
        raise NoAcquisitionCapabilityError("browser extra is unavailable")


@pytest.mark.asyncio
async def test_hybrid_keeps_static_response_when_rendering_is_unavailable() -> None:
    result = await HybridAcquisition(_StaticShell(), _MissingRendered()).fetch(
        FetchRequest("https://example.test/", RenderingRequirement.PREFERRED)
    )

    assert result.fetch_method == "http"
    assert result.rendering_fallback_failed
    assert b'id="app"' in result.body


@pytest.mark.asyncio
async def test_hybrid_falls_back_when_static_acquisition_fails() -> None:
    class _StaticUnavailable:
        async def fetch(self, request: FetchRequest) -> FetchResponse:
            raise AcquisitionError(
                "http_fetch_failed", "static transport is unavailable", retryable=True
            )

    result = await HybridAcquisition(_StaticUnavailable(), _RenderedPage()).fetch(
        FetchRequest("https://example.test/", RenderingRequirement.PREFERRED)
    )

    assert result.fetch_method == "playwright"
    assert b"Rendered content" in result.body


@pytest.mark.asyncio
async def test_hybrid_static_mode_does_not_use_rendered_fallback() -> None:
    class _StaticUnavailable:
        async def fetch(self, request: FetchRequest) -> FetchResponse:
            raise AcquisitionError(
                "http_fetch_failed", "static transport is unavailable", retryable=True
            )

    rendered = _RenderedPage()
    acquisition = HybridAcquisition(_StaticUnavailable(), rendered)

    with pytest.raises(AcquisitionError, match="static transport is unavailable"):
        await acquisition.fetch(
            FetchRequest("https://example.test/", RenderingRequirement.STATIC)
        )


@pytest.mark.asyncio
async def test_playwright_initializes_one_browser_for_concurrent_calls(
    monkeypatch,
) -> None:
    try:
        import playwright.async_api as playwright_api
    except ImportError:
        pytest.skip("Playwright package is not installed in this environment")

    class _Browser:
        async def close(self) -> None:
            return

    class _Launcher:
        def __init__(self, browser: _Browser) -> None:
            self.browser = browser
            self.launch_count = 0

        async def launch(self, *, headless: bool) -> _Browser:
            self.launch_count += 1
            await asyncio.sleep(0)
            return self.browser

    class _Manager:
        def __init__(self) -> None:
            self.start_count = 0
            self.chromium = _Launcher(_Browser())

        async def start(self):
            self.start_count += 1
            await asyncio.sleep(0)
            return self

        async def stop(self) -> None:
            return

    manager = _Manager()
    monkeypatch.setattr(playwright_api, "async_playwright", lambda: manager)
    acquisition = PlaywrightAcquisition()

    browsers = await asyncio.gather(*(acquisition._ensure_browser() for _ in range(4)))
    await acquisition.close()

    assert manager.start_count == 1
    assert manager.chromium.launch_count == 1
    assert all(browser is browsers[0] for browser in browsers)
