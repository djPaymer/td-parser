"""Process one manufacturer site end to end: reachability -> regex -> product URLs -> product pages.

Site statuses written to the summary sheet:

* ``ok``           products with data were collected
* ``empty``        the site was read but has nothing usable: no product-like URL
                   shapes, no product links, or product pages without description
                   and specs (JavaScript cards even the browser could not read)
* ``unavailable``  the home page does not open: DNS, TLS, connection, 403, dead
                   domain.  Decided before the site takes a concurrency slot.
* ``error``        something else went wrong; the note holds the reason
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.agents.llm import LlmClient, LlmError
from app.agents.pattern import AgentError, EmptyHtml, NoProductShapes, PatternAgent, PatternResult
from app.clients.browser import BrowserPool
from app.clients.http.fetch import Fetcher, HtmlFetchError, HtmlFetcher
from app.clients.http.urls import origin
from app.core.config import Settings
from app.excel import Manufacturer, SiteReport
from app.parsers.collect import ProductCollector
from app.parsers.product import ProductData, extract_product

log = logging.getLogger(__name__)

PROGRESS_EVERY = 25
HTTP, BROWSER = "http", "browser"


def make_fetcher(settings: Settings, *, timeout: float | None = None) -> HtmlFetcher:
    return HtmlFetcher(
        timeout=settings.fetch.timeout if timeout is None else timeout,
        user_agent=settings.fetch.user_agent,
        max_bytes=settings.fetch.max_bytes,
        delay_seconds=settings.fetch.delay_seconds,
        verify_ssl=settings.fetch.verify_ssl,
    )


def make_llm(settings: Settings) -> LlmClient | None:
    if not settings.agent.api_key:
        log.warning("APP_CONFIG__AGENT__API_KEY is empty: product shapes are chosen heuristically")
        return None
    try:
        return LlmClient(settings.agent)
    except LlmError as exc:
        log.warning("LLM disabled: %s", exc)
        return None


async def make_browser(settings: Settings) -> BrowserPool | None:
    pool = BrowserPool(settings.browser, settings.fetch.user_agent)
    return pool if await pool.start() else None


# ----------------------------------------------------------------------------- preflight


@dataclass(slots=True)
class Preflight:
    manufacturer: Manufacturer
    mode: str  # HTTP | BROWSER | "unavailable"
    note: str = ""

    @property
    def reachable(self) -> bool:
        return self.mode in (HTTP, BROWSER)


async def preflight(manufacturer: Manufacturer, settings: Settings, browser: BrowserPool | None) -> Preflight:
    """Open the home page once with a short timeout, so dead sites never occupy a worker slot.

    A 403 / TLS / connection failure over plain HTTP is retried in the browser
    when one is running; if that works the whole site is processed in browser mode.
    """

    site = manufacturer.url
    http_error = ""
    # a slow site gets a second, patient attempt: a false "unavailable" loses the whole site
    for timeout in (settings.fetch.preflight_timeout, settings.fetch.timeout):
        fetcher = make_fetcher(settings, timeout=timeout)
        try:
            await fetcher.get(site, origin=site)
            return Preflight(manufacturer, HTTP)
        except HtmlFetchError as exc:
            http_error = str(exc)
        finally:
            await fetcher.aclose()
        if _definitive(http_error):
            break

    if browser is None or not browser.available:
        log.warning("%s: unavailable (%s)", site, http_error)
        return Preflight(manufacturer, "unavailable", http_error)
    probe = browser.fetcher(0)
    try:
        await probe.get(site, origin=site)
    except HtmlFetchError as exc:
        log.warning("%s: unavailable (%s; browser: %s)", site, http_error, exc)
        return Preflight(manufacturer, "unavailable", f"{http_error}; browser: {exc}")
    finally:
        await probe.aclose()
    log.info("%s: http failed (%s), the browser opens it; processing in browser mode", site, http_error)
    return Preflight(manufacturer, BROWSER, f"browser mode: http failed ({http_error})")


def _definitive(error: str) -> bool:
    """An HTTP status or an unresolvable name will not change on a retry; timeouts might."""

    return error.startswith("HTTP ") or "getaddrinfo" in error or "NAME_NOT_RESOLVED" in error


# ----------------------------------------------------------------------------- one site


@dataclass(slots=True)
class _Session:
    """The fetcher a site is currently read with; switches http -> browser at most once per stage."""

    settings: Settings
    browser: BrowserPool | None
    mode: str
    fetcher: Fetcher
    notes: list[str] = field(default_factory=list)

    @property
    def can_switch(self) -> bool:
        return self.mode == HTTP and self.browser is not None and self.browser.available

    async def switch(self, target: str, reason: str) -> None:
        await self.fetcher.aclose()
        if target == BROWSER:
            assert self.browser is not None
            self.fetcher = self.browser.fetcher(self.settings.fetch.delay_seconds)
        else:
            self.fetcher = make_fetcher(self.settings)
        self.mode = target
        self.notes.append(f"{target}: {reason}")

    async def aclose(self) -> None:
        await self.fetcher.aclose()


def _open_session(settings: Settings, browser: BrowserPool | None, mode: str) -> _Session:
    if mode == BROWSER and browser is not None and browser.available:
        return _Session(settings, browser, BROWSER, browser.fetcher(settings.fetch.delay_seconds))
    return _Session(settings, browser, HTTP, make_fetcher(settings))


async def process_site(
    manufacturer: Manufacturer,
    settings: Settings,
    llm: LlmClient | None,
    browser: BrowserPool | None = None,
    *,
    mode: str = HTTP,
    max_products: int | None = None,
) -> SiteReport:
    site = manufacturer.url
    started = time.monotonic()
    # 0 = no limit: every product link the collector finds is downloaded
    limit = settings.parse.max_products if max_products is None else max_products
    session = _open_session(settings, browser, mode)
    if session.mode == BROWSER:
        session.notes.append("browser mode")
    report = SiteReport(manufacturer=manufacturer.name, site=site, status="error", products=[])
    try:
        agent = PatternAgent(settings.agent, llm)
        try:
            result = await _build_pattern(agent, site, session)
        except NoProductShapes as exc:
            report.status = "empty"
            report.note = _join(session.notes, str(exc))
            log.info("%s: empty, %s", site, exc)
            return report
        except (AgentError, HtmlFetchError) as exc:
            report.note = _join(session.notes, str(exc))
            log.error("%s: %s", site, exc)
            return report

        pattern = result.pattern
        report.regex = pattern.regex
        report.source = pattern.source
        report.confidence = pattern.confidence
        report.pages = len(result.crawl.pages)
        catalog = result.crawl.site_url  # may differ from ``site`` (redirect / landing page)
        if result.crawl.moved_reason:
            session.notes.append(f"catalog at {catalog} ({result.crawl.moved_reason})")

        collector = ProductCollector(
            catalog,
            pattern,
            session.fetcher,
            max_listing_pages=settings.parse.max_listing_pages,
            max_pages_per_listing=settings.parse.max_pages_per_listing,
            max_products=limit,
            max_stale_pages=settings.parse.max_stale_pages,
        )
        collected = await collector.run(result.crawl.pages, result.listing_pages)
        report.pages += collected.pages_visited
        if not collected.products:
            report.status = "empty"
            report.note = _join(session.notes, "regex derived but no product links collected")
            return report

        products = list(collected.products.items())
        total = len(products)
        failures = await _download_products(site, catalog, products, session, report)

        with_desc = sum(1 for p in report.products if p.description)
        with_specs = sum(1 for p in report.products if p.specs or p.specs_table)
        notes = list(session.notes)
        notes.append(f"shapes: {', '.join(pattern.shapes)}")
        if pattern.page_param:
            notes.append(f"pager param: {pattern.page_param}")
        if pattern.reason:
            notes.append(pattern.reason)
        if failures:
            notes.append(f"{failures} product pages failed to download")
        if limit and total >= limit:
            notes.append(f"stopped at the limit of {limit} products")
        notes.append(f"description: {with_desc}/{total}, specs: {with_specs}/{total}")
        if with_desc == 0 and with_specs == 0:
            # URLs matched the regex but the pages carry nothing: cards drawn by JavaScript
            # that even the browser did not get, or the shape was not products at all
            report.status = "empty"
            notes.insert(0, "product pages have neither description nor specs")
        else:
            report.status = "ok"
        report.note = "; ".join(notes)
        log.info(
            "%s: %s, %d products in %.0fs (%s)", site, report.status, total, time.monotonic() - started, report.note
        )
        return report
    finally:
        await session.aclose()


async def _build_pattern(agent: PatternAgent, site: str, session: _Session) -> PatternResult:
    """Derive the regex; an HTML without links or a 403 wall is retried once in the browser."""

    try:
        return await agent.build_pattern(site, session.fetcher)
    except (EmptyHtml, HtmlFetchError) as exc:
        if not session.can_switch:
            raise
        log.info("%s: %s; retrying in the browser", site, exc)
        await session.switch(BROWSER, f"http gave {_short(exc)}")
    return await agent.build_pattern(site, session.fetcher)


async def _download_products(
    site: str,
    catalog: str,
    products: list[tuple[str, str]],
    session: _Session,
    report: SiteReport,
) -> int:
    """Fill ``report.products``; returns the number of pages that failed to download.

    The first ``browser.probe_products`` cards are checked: if none has a
    description or specs over plain HTTP, the cards are drawn by JavaScript and
    the rest of the site is read in the browser (falling back to http again if
    the browser did not help either).
    """

    total = len(products)
    probe = session.settings.browser.probe_products
    failures = 0
    site_origin = origin(catalog)
    for index, (url, name) in enumerate(products, start=1):
        data, ok = await _fetch_product(session.fetcher, url, name, site_origin)
        failures += 0 if ok else 1
        if ok:
            report.pages += 1
        report.products.append(data)
        if index == probe and session.can_switch and not any(_has_content(p) for p in report.products):
            log.info("%s: first %d product pages are empty over http; re-reading them in the browser", site, probe)
            await session.switch(BROWSER, f"product pages empty over http, {probe} probed")
            retried: list[ProductData] = []
            for retry_url, retry_name in products[:probe]:
                data, ok = await _fetch_product(session.fetcher, retry_url, retry_name, site_origin)
                if not ok:
                    break  # the browser cannot even open the page; do not burn a timeout per card
                retried.append(data)
            if any(_has_content(p) for p in retried):
                report.products = retried + report.products[len(retried):]
                report.pages += len(retried)
            else:
                log.info("%s: the browser did not help; staying with http", site)
                await session.switch(HTTP, "browser gave empty product pages too")
        if index % PROGRESS_EVERY == 0 or index == total:
            log.info("%s: product pages %d/%d", site, index, total)
    return failures


async def _fetch_product(fetcher: Fetcher, url: str, name: str, site_origin: str) -> tuple[ProductData, bool]:
    try:
        page = await fetcher.get(url, origin=site_origin)
    except HtmlFetchError as exc:
        log.info("product page failed %s (%s)", url, exc)
        return ProductData(url=url, name=name), False
    return extract_product(page.html, page.url, name), True


def _has_content(product: ProductData) -> bool:
    return bool(product.description or product.specs or product.specs_table)


def _join(notes: list[str], text: str) -> str:
    return "; ".join([*notes, text])


def _short(exc: BaseException) -> str:
    return str(exc).split(";")[0][:120]
