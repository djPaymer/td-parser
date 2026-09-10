"""Headless-browser fetcher (Playwright / Chromium) for sites that plain HTTP cannot read.

Used as a fallback only: when a home page has no links in its HTML (the catalog
is rendered by JavaScript), when product pages come back without description
and specs, or when a site answers 403 to httpx but serves a real browser.

One Chromium process is shared by the whole run (``BrowserPool``); every site
gets its own browser context (cookies, referer) through ``BrowserPool.fetcher``.
Images, fonts and media are blocked to keep page loads short.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.clients.http.fetch import HtmlFetchError, Page
from app.clients.http.urls import host_key, origin as origin_of, same_host
from app.core.config import BrowserConfig

log = logging.getLogger(__name__)

BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}
INSTALL_HINT = "install with: poetry run playwright install chromium"


class BrowserPool:
    def __init__(self, config: BrowserConfig, user_agent: str) -> None:
        self._config = config
        self._user_agent = user_agent
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._browser is not None

    async def start(self) -> bool:
        """Launch Chromium; returns False (and logs why) when Playwright or the browser is missing."""

        if not self._config.enabled:
            return False
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.warning("browser fallback disabled: playwright is not installed (poetry install)")
            return False
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # playwright raises its own Error hierarchy
            log.warning("browser fallback disabled: %s (%s)", str(exc).splitlines()[0][:160], INSTALL_HINT)
            await self.aclose()
            return False
        log.info("browser fallback ready (Chromium %s)", self._browser.version)
        return True

    async def aclose(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # shutting down anyway
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def fetcher(self, delay_seconds: float) -> BrowserFetcher:
        if self._browser is None:
            raise HtmlFetchError("browser is not running")
        return BrowserFetcher(self, delay_seconds)

    async def new_context(self) -> Any:
        assert self._browser is not None
        async with self._lock:
            context = await self._browser.new_context(
                user_agent=self._user_agent,
                locale="en-US",
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=True,
            )
        await context.route("**/*", _block_heavy_resources)
        return context

    @property
    def timeout_ms(self) -> int:
        return int(self._config.timeout * 1000)

    @property
    def settle_ms(self) -> int:
        return int(self._config.settle_seconds * 1000)


async def _block_heavy_resources(route: Any) -> None:
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


class BrowserFetcher:
    """``Fetcher`` implementation on top of one browser context (one site)."""

    def __init__(self, pool: BrowserPool, delay_seconds: float) -> None:
        self._pool = pool
        self._delay = delay_seconds
        self._context: Any = None
        self._requests = 0

    async def get(self, url: str, *, origin: str | None = None) -> Page:
        if origin and not same_host(origin, url):
            raise HtmlFetchError(f"refusing other host: {url}")
        if self._context is None:
            self._context = await self._pool.new_context()
        if self._delay and self._requests:
            await asyncio.sleep(self._delay)
        self._requests += 1
        page = await self._context.new_page()
        try:
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._pool.timeout_ms,
                    referer=_referer(url, origin),
                )
            except Exception as exc:
                raise HtmlFetchError(f"browser GET {url}: {_short(exc)}") from exc
            status = response.status if response is not None else 0
            if status >= 400:
                raise HtmlFetchError(f"HTTP {status} GET {url} (browser)")
            content_type = (response.headers.get("content-type") or "").lower() if response is not None else ""
            if content_type and "html" not in content_type and "xml" not in content_type and "text" not in content_type:
                raise HtmlFetchError(f"not HTML ({content_type.split(';')[0]}) GET {url}")
            # single-page apps fill the DOM after the load event; give them a bounded moment
            try:
                await page.wait_for_load_state("networkidle", timeout=min(self._pool.timeout_ms, 10_000))
            except Exception:
                pass
            if self._pool.settle_ms:
                await page.wait_for_timeout(self._pool.settle_ms)
            html = await page.content()
            return Page(url=page.url, status=status or 200, html=html)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def aclose(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None


def _referer(url: str, site: str | None) -> str:
    try:
        return origin_of(site or url) + "/"
    except ValueError:
        return f"https://{host_key(url)}/"


def _short(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:200]
