"""Command line interface.

    python -m app run manufacturers.xlsx products.xlsx [--max-products N] [--concurrency N] [--no-llm]
    python -m app pattern https://www.example.com

``run`` reads manufacturer sites from the first sheet of the input workbook
(a column whose header mentions url/site/сайт, or any cell that looks like a
URL) and writes two sheets: ``Товары`` (one row per product) and ``Сводка``
(one row per site with the derived regex and status).  The output file is
saved after every finished site, so a long run can be inspected while it
works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict

from app.core.config import settings
from app.excel import Manufacturer, ResultWorkbook, SiteReport, normalize_site_url, read_manufacturers
from app.runner import make_browser, make_fetcher, make_llm, preflight, process_site

log = logging.getLogger("app")

PREFLIGHT_CONCURRENCY = 8  # home-page reachability checks in flight at once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="td-parser", description="Collect products from manufacturer sites")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Excel with manufacturers -> Excel with products")
    run.add_argument("input", help="xlsx with manufacturer sites")
    run.add_argument("output", help="xlsx to write products into")
    run.add_argument(
        "--max-products",
        type=int,
        default=None,
        help=f"cap per site, 0 = all found (default {settings.parse.max_products or 'all'})",
    )
    run.add_argument("--concurrency", type=int, default=None, help=f"sites in parallel (default {settings.parse.concurrency})")
    run.add_argument("--no-llm", action="store_true", help="choose product shapes heuristically, do not call the LLM")
    run.add_argument("--no-browser", action="store_true", help="never fall back to headless Chromium")

    pattern = sub.add_parser("pattern", help="derive and print the product regex of one site")
    pattern.add_argument("url")
    pattern.add_argument("--no-llm", action="store_true")
    pattern.add_argument("--browser", action="store_true", help="read the site with headless Chromium instead of httpx")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    if args.command == "run":
        return asyncio.run(run_command(args))
    return asyncio.run(pattern_command(args))


async def run_command(args: argparse.Namespace) -> int:
    manufacturers = read_manufacturers(args.input)
    if not manufacturers:
        log.error("%s: no manufacturer URLs found", args.input)
        return 2
    log.info("%d manufacturers from %s", len(manufacturers), args.input)
    llm = None if args.no_llm else make_llm(settings)
    browser = None if args.no_browser else await make_browser(settings)
    semaphore = asyncio.Semaphore(args.concurrency or settings.parse.concurrency)
    preflight_slots = asyncio.Semaphore(PREFLIGHT_CONCURRENCY)

    async def guarded(item: Manufacturer) -> SiteReport:
        # the reachability check runs outside the worker semaphore: a dead domain, a TLS
        # failure or a 403 wall is reported at once and never blocks a real site
        async with preflight_slots:
            check = await preflight(item, settings, browser)
        if not check.reachable:
            return SiteReport(item.name, item.url, "unavailable", [], note=check.note)
        async with semaphore:
            try:
                report = await process_site(
                    item, settings, llm, browser, mode=check.mode, max_products=args.max_products
                )
            except Exception as exc:  # one broken site must not abort the batch
                log.exception("%s: unexpected failure", item.url)
                return SiteReport(item.name, item.url, "error", [], note=f"{type(exc).__name__}: {exc}")
        if check.note and check.note not in report.note:
            report.note = "; ".join(filter(None, [check.note, report.note]))
        return report

    reports: dict[str, SiteReport] = {}
    try:
        tasks = {asyncio.create_task(guarded(item)): item for item in manufacturers}
        for task in asyncio.as_completed(tasks):
            report = await task
            reports[report.site] = report
            workbook = ResultWorkbook(args.output)  # rebuilt each time so rows keep the input order
            for item in manufacturers:
                if item.url in reports:
                    workbook.add(reports[item.url])
            workbook.save()
            log.info("saved %s (%d/%d sites)", args.output, len(reports), len(manufacturers))
    finally:
        if browser is not None:
            await browser.aclose()

    by_status = Counter(r.status for r in reports.values())
    products = sum(len(r.products) for r in reports.values())
    log.info(
        "finished: %d/%d sites ok (%s), %d products -> %s",
        by_status.get("ok", 0),
        len(manufacturers),
        ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())),
        products,
        args.output,
    )
    return 0 if by_status.get("ok") else 1


async def pattern_command(args: argparse.Namespace) -> int:
    from app.agents.pattern import AgentError, PatternAgent
    from app.clients.http.fetch import HtmlFetchError

    url = normalize_site_url(args.url)
    if not url:
        log.error("not a site URL: %s", args.url)
        return 2
    llm = None if args.no_llm else make_llm(settings)
    browser = None
    if args.browser:
        browser = await make_browser(settings)
        if browser is None:
            return 2
        fetcher = browser.fetcher(settings.fetch.delay_seconds)
    else:
        fetcher = make_fetcher(settings)
    try:
        result = await PatternAgent(settings.agent, llm).build_pattern(url, fetcher)
    except (AgentError, HtmlFetchError) as exc:
        log.error("%s: %s", url, exc)
        return 1
    finally:
        await fetcher.aclose()
        if browser is not None:
            await browser.aclose()
    payload = {
        "site": url,
        "catalog": result.crawl.site_url,
        "moved": result.crawl.moved_reason,
        "pattern": asdict(result.pattern),
        "listing_pages": result.listing_pages,
        "meta": result.meta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
