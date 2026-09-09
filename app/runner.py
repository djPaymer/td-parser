"""Process one manufacturer site end to end: regex -> product URLs -> product pages."""

from __future__ import annotations

import logging
import time

from app.agents.llm import LlmClient, LlmError
from app.agents.pattern import AgentError, PatternAgent
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher
from app.clients.http.urls import origin
from app.core.config import Settings
from app.excel import Manufacturer, SiteReport
from app.parsers.collect import ProductCollector
from app.parsers.product import ProductData, extract_product

log = logging.getLogger(__name__)

PROGRESS_EVERY = 25


def make_fetcher(settings: Settings) -> HtmlFetcher:
    return HtmlFetcher(
        timeout=settings.fetch.timeout,
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


async def process_site(
    manufacturer: Manufacturer,
    settings: Settings,
    llm: LlmClient | None,
    *,
    max_products: int | None = None,
) -> SiteReport:
    site = manufacturer.url
    started = time.monotonic()
    # 0 = no limit: every product link the collector finds is downloaded
    limit = settings.parse.max_products if max_products is None else max_products
    fetcher = make_fetcher(settings)
    report = SiteReport(manufacturer=manufacturer.name, site=site, status="error", products=[])
    try:
        try:
            result = await PatternAgent(settings.agent, llm).build_pattern(site, fetcher)
        except (AgentError, HtmlFetchError) as exc:
            report.note = str(exc)
            log.error("%s: %s", site, exc)
            return report
        pattern = result.pattern
        report.regex = pattern.regex
        report.source = pattern.source
        report.confidence = pattern.confidence
        report.pages = len(result.crawl.pages)

        collector = ProductCollector(
            site,
            pattern,
            fetcher,
            max_listing_pages=settings.parse.max_listing_pages,
            max_pages_per_listing=settings.parse.max_pages_per_listing,
            max_products=limit,
        )
        collected = await collector.run(result.crawl.pages, result.listing_pages)
        report.pages += collected.pages_visited
        if not collected.products:
            report.status = "empty"
            report.note = "regex derived but no product links collected"
            return report

        failures = 0
        total = len(collected.products)
        for index, (url, name) in enumerate(collected.products.items(), start=1):
            try:
                page = await fetcher.get(url, origin=origin(site))
            except HtmlFetchError as exc:
                failures += 1
                log.info("%s: product page failed %s (%s)", site, url, exc)
                report.products.append(ProductData(url=url, name=name))
                continue
            report.pages += 1
            report.products.append(extract_product(page.html, page.url, name))
            if index % PROGRESS_EVERY == 0 or index == total:
                log.info("%s: product pages %d/%d", site, index, total)

        report.status = "ok"
        notes = [f"shapes: {', '.join(pattern.shapes)}"]
        if pattern.page_param:
            notes.append(f"pager param: {pattern.page_param}")
        if pattern.reason:
            notes.append(pattern.reason)
        if failures:
            notes.append(f"{failures} product pages failed to download")
        if limit and total >= limit:
            notes.append(f"stopped at the limit of {limit} products")
        with_desc = sum(1 for p in report.products if p.description)
        with_specs = sum(1 for p in report.products if p.specs or p.specs_table)
        notes.append(f"description: {with_desc}/{total}, specs: {with_specs}/{total}")
        report.note = "; ".join(notes)
        log.info(
            "%s: done, %d products in %.0fs (%s)", site, total, time.monotonic() - started, report.note
        )
        return report
    finally:
        await fetcher.aclose()
