"""Playwright and hybrid acquisition adapters."""

from __future__ import annotations

import asyncio
import time

from ..contracts import RenderingRequirement, Scope
from ..errors import AcquisitionError, NoAcquisitionCapabilityError
from ..ports import AcquisitionPort, FetchRequest, FetchResponse
from ..url_policy import UrlPolicy


class PlaywrightAcquisition:
    """Render a public page in a fresh browser context.

    ``browser_endpoint`` may be a CDP endpoint for a dedicated browser node.
    The adapter never accepts or creates a personal browser profile.
    """

    def __init__(
        self,
        *,
        browser_endpoint: str | None = None,
        browser_name: str = "chromium",
        headless: bool = True,
        timeout_seconds: float = 30.0,
        settle_timeout_seconds: float = 2.0,
        max_response_bytes: int = 8_000_000,
        user_agent: str = "website-collection-kit/0.1",
    ) -> None:
        if browser_name not in {"chromium", "firefox", "webkit"}:
            raise ValueError("browser_name must be chromium, firefox, or webkit")
        if browser_endpoint and browser_name != "chromium":
            raise ValueError("CDP endpoints require the chromium adapter")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if settle_timeout_seconds < 0:
            raise ValueError("settle_timeout_seconds must not be negative")
        if max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")
        self.browser_endpoint = browser_endpoint
        self.browser_name = browser_name
        self.headless = headless
        self.timeout_ms = max(1, int(timeout_seconds * 1000))
        self.settle_timeout_ms = max(0, int(settle_timeout_seconds * 1000))
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    async def _ensure_browser(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise NoAcquisitionCapabilityError(
                "PlaywrightAcquisition requires the 'browser' extra"
            ) from exc
        if self._browser is not None:
            return self._browser
        async with self._browser_lock:
            if self._browser is not None:
                return self._browser
            playwright = await async_playwright().start()
            try:
                if self.browser_endpoint:
                    browser = await playwright.chromium.connect_over_cdp(
                        self.browser_endpoint
                    )
                else:
                    launcher = getattr(playwright, self.browser_name)
                    browser = await launcher.launch(headless=self.headless)
            except BaseException:
                await playwright.stop()
                raise
            self._playwright = playwright
            self._browser = browser
            return browser

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        browser = await self._ensure_browser()
        started = time.perf_counter()
        context = None
        blocked_navigation_url: str | None = None
        try:
            scope = request.scope or Scope.for_seeds([request.url])
            policy = UrlPolicy(scope)
            context = await browser.new_context(user_agent=self.user_agent)
            page = await context.new_page()

            async def guard_route(route, browser_request) -> None:
                nonlocal blocked_navigation_url
                is_navigation = browser_request.is_navigation_request()
                decision = (
                    policy.decide(browser_request.url)
                    if is_navigation
                    else policy.decide_auxiliary(browser_request.url)
                )
                if decision.accepted:
                    await route.continue_()
                    return
                if is_navigation:
                    blocked_navigation_url = browser_request.url
                await route.abort()

            # Browser navigation follows redirects internally.  Intercepting
            # requests here is what makes the core's Scope meaningful for CDP
            # runs as well as for the no-redirect HTTP adapter.  Subresources
            # may use another path on an allowed host, but never another host.
            await page.route("**/*", guard_route)
            response = await page.goto(
                request.url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            if self.settle_timeout_ms:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError

                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=min(self.timeout_ms, self.settle_timeout_ms),
                    )
                except PlaywrightTimeoutError:
                    # Long-polling and analytics connections must not prevent
                    # returning the DOM that is available within the cap.
                    pass
            html = await page.content()
            body = html.encode("utf-8")
            if len(body) > self.max_response_bytes:
                raise AcquisitionError(
                    "response_too_large",
                    "rendered response exceeds configured size limit",
                )
            status = response.status if response is not None else 200
            headers = await response.all_headers() if response is not None else {}
            return FetchResponse(
                requested_url=request.url,
                final_url=page.url,
                status=status,
                headers=dict(headers),
                body=body,
                media_type="text/html",
                fetch_method="playwright_cdp"
                if self.browser_endpoint
                else "playwright",
                elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                rendering_fallback_failed=False,
            )
        except AcquisitionError:
            raise
        except Exception as exc:
            if blocked_navigation_url:
                return FetchResponse(
                    requested_url=request.url,
                    final_url=request.url,
                    status=302,
                    headers={},
                    body=b"",
                    media_type="text/html",
                    fetch_method="playwright_cdp"
                    if self.browser_endpoint
                    else "playwright",
                    elapsed_ms=max(0, int((time.perf_counter() - started) * 1000)),
                    redirect_to=blocked_navigation_url,
                    rendering_fallback_failed=False,
                )
            raise AcquisitionError(
                "browser_fetch_failed", str(exc), retryable=True
            ) from exc
        finally:
            if context is not None:
                await context.close()

    async def close(self) -> None:
        first_error: Exception | None = None
        async with self._browser_lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception as exc:  # noqa: BLE001 - still stop Playwright after browser cleanup fails
                    first_error = exc
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as exc:  # noqa: BLE001 - preserve cleanup of both resources
                    first_error = first_error or exc
                self._playwright = None
        if first_error is not None:
            raise first_error


class HybridAcquisition:
    """Select static or rendered acquisition behind one port."""

    def __init__(self, static: AcquisitionPort, rendered: AcquisitionPort) -> None:
        self.static = static
        self.rendered = rendered

    async def fetch(self, request: FetchRequest) -> FetchResponse:
        if request.rendering is RenderingRequirement.REQUIRED:
            return await self.rendered.fetch(request)
        if request.rendering is RenderingRequirement.STATIC:
            return await self.static.fetch(request)
        try:
            response = await self.static.fetch(request)
        except (AcquisitionError, NoAcquisitionCapabilityError) as static_error:
            # A public site can be reachable from a browser while the static
            # client is rejected by TLS, transport, or a browser-sensitive
            # edge rule.  In preferred mode this is exactly the case where
            # rendering is a useful recovery path.  If it also fails, keep
            # the original static error so the caller receives the stable
            # failure it would have seen without the optional fallback.
            try:
                return await self.rendered.fetch(request)
            except (AcquisitionError, NoAcquisitionCapabilityError):
                raise static_error
        if (
            request.rendering is RenderingRequirement.PREFERRED
            and _looks_like_app_shell(response.body, response.media_type)
        ):
            try:
                return await self.rendered.fetch(request)
            except (AcquisitionError, NoAcquisitionCapabilityError):
                # The static response remains usable; its method is evidence of
                # the fallback failure and is not hidden as a successful render.
                return FetchResponse(
                    requested_url=response.requested_url,
                    final_url=response.final_url,
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                    media_type=response.media_type,
                    fetch_method=response.fetch_method,
                    elapsed_ms=response.elapsed_ms,
                    redirect_to=response.redirect_to,
                    rendering_fallback_failed=True,
                )
        return response

    async def close(self) -> None:
        first_error: Exception | None = None
        for adapter in (self.static, self.rendered):
            close = getattr(adapter, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:  # noqa: BLE001 - close all selected adapters before reporting failure
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _looks_like_app_shell(body: bytes, media_type: str) -> bool:
    if media_type and not media_type.startswith(("text/html", "application/xhtml")):
        return False
    text = body[:100_000].decode("utf-8", errors="ignore").lower()
    if not text:
        return False
    has_app_marker = any(
        marker in text
        for marker in (
            'id="app"',
            "id='app'",
            'id="root"',
            "id='root'",
            "__next_data__",
            "data-reactroot",
        )
    )
    visible = len(" ".join(part for part in text.split("<script")[:1] if part).strip())
    return has_app_marker and visible < 1200 and text.count("<script") >= 1
