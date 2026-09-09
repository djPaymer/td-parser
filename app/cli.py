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
from dataclasses import asdict

from app.core.config import settings
from app.excel import Manufacturer, ResultWorkbook, SiteReport, normalize_site_url, read_manufacturers
from app.runner import make_fetcher, make_llm, process_site

log = logging.getLogger("app")


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

    pattern = sub.add_parser("pattern", help="derive and print the product regex of one site")
    pattern.add_argument("url")
    pattern.add_argument("--no-llm", action="store_true")
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
    semaphore = asyncio.Semaphore(args.concurrency or settings.parse.concurrency)

    async def guarded(item: Manufacturer) -> SiteReport:
        async with semaphore:
            try:
                return await process_site(item, settings, llm, max_products=args.max_products)
            except Exception as exc:  # one broken site must not abort the batch
                log.exception("%s: unexpected failure", item.url)
                return SiteReport(item.name, item.url, "error", [], note=f"{type(exc).__name__}: {exc}")

    reports: dict[str, SiteReport] = {}
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

    ok = sum(1 for r in reports.values() if r.status == "ok")
    products = sum(len(r.products) for r in reports.values())
    log.info("finished: %d/%d sites ok, %d products -> %s", ok, len(manufacturers), products, args.output)
    return 0 if ok else 1


async def pattern_command(args: argparse.Namespace) -> int:
    from app.agents.pattern import AgentError, PatternAgent
    from app.clients.http.fetch import HtmlFetchError

    url = normalize_site_url(args.url)
    if not url:
        log.error("not a site URL: %s", args.url)
        return 2
    llm = None if args.no_llm else make_llm(settings)
    fetcher = make_fetcher(settings)
    try:
        result = await PatternAgent(settings.agent, llm).build_pattern(url, fetcher)
    except (AgentError, HtmlFetchError) as exc:
        log.error("%s: %s", url, exc)
        return 1
    finally:
        await fetcher.aclose()
    payload = {"site": url, "pattern": asdict(result.pattern), "listing_pages": result.listing_pages, "meta": result.meta}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
