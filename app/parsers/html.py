from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import urlparse, urlunparse

from app.clients.http.fetch import HtmlFetchError, HtmlFetcher
from app.clients.http.urls import abs_url, origin
from app.parsers.links import compile_regex, extract_links, is_content_path, iter_anchors
from app.parsers.paginate import detect_page_param
from app.parsers.paginate import page_url as with_page
from app.parsers.schema import LinksSpec, SiteInstruction

log = logging.getLogger(__name__)

MAX_LISTINGS = 80  # category pages followed when the instruction has no `categories`
MAX_PAGES_PER_LISTING = 50
MAX_TOTAL_PAGES = 800


class ParseError(RuntimeError):
    pass


async def parse_site(site_url: str, instruction: SiteInstruction, fetcher: HtmlFetcher) -> dict:
    if (instruction.engine or "html").lower() != "html":
        raise ParseError(f"unsupported engine {instruction.engine!r}")
    base = origin(site_url)
    listing = abs_url(base, instruction.url or "/")
    declared = instruction.pagination.param if instruction.pagination else None
    product_re = compile_regex(instruction.links.href, "links.href")
    category_re = compile_regex(instruction.categories.href, "categories.href") if instruction.categories else None
    max_listings = instruction.categories.max_pages if instruction.categories else MAX_LISTINGS

    first = await fetcher.get(listing, origin=base)
    queue: deque[str] = deque([listing])
    queued = {_without_query(listing)}
    seed_html: dict[str, str] = {listing: first.html}
    products: dict[str, dict] = {}
    listings_done = 0
    pages = 0
    while queue and listings_done < max_listings and pages < MAX_TOTAL_PAGES:
        list_url = queue.popleft()
        try:
            chunk, n, first_html = await _paginate(
                fetcher, list_url, base, instruction.links, declared, seed_html=seed_html.pop(list_url, None)
            )
        except HtmlFetchError as exc:
            if list_url == listing:
                raise
            log.info("parse %s: skip listing %s (%s)", base, list_url, exc)
            continue
        listings_done += 1
        pages += n
        for item in chunk:
            products.setdefault(item["url"] or "", item)
        for url in _sublistings(first_html, list_url, listing, base, product_re, category_re):
            key = _without_query(url)
            if key in queued:
                continue
            queued.add(key)
            queue.append(url)
    if not products:
        raise ParseError(f"no product links on {listing}")
    return {
        "site": base,
        "listing": listing,
        "listings": listings_done,
        "pages": pages,
        "total_products": len(products),
        "products": list(products.values()),
    }


def _path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def _without_query(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _is_product(product_re: re.Pattern[str] | None, href: str, url: str, path: str) -> bool:
    return bool(product_re and (product_re.search(href) or product_re.search(url) or product_re.search(path)))


def _sublistings(
    html: str,
    page_url: str,
    root_url: str,
    base: str,
    product_re: re.Pattern[str] | None,
    category_re: re.Pattern[str] | None,
) -> list[str]:
    """Category / listing pages linked from this listing that should be crawled too.

    With ``categories.href`` any link matching it (and not matching the product
    regex) is followed.  Without it the legacy rule applies: one level of links
    directly below the catalog entry path.
    """

    found: list[str] = []
    seen: set[str] = set()
    root = _path(root_url)
    prefix = root + "/" if root else None
    for anchor in iter_anchors(html, page_url):
        path = anchor.path
        if not is_content_path(path) or _is_product(product_re, anchor.href, anchor.url, path):
            continue
        if category_re is not None:
            if not (category_re.search(anchor.href) or category_re.search(anchor.url) or category_re.search(path)):
                continue
        elif prefix is None or not path.startswith(prefix):
            continue
        key = _without_query(anchor.url)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


async def _paginate(
    fetcher: HtmlFetcher,
    listing_url: str,
    base: str,
    spec: LinksSpec,
    page_param: str | None,
    seed_html: str | None = None,
) -> tuple[list[dict], int, str]:
    """Walk the pager of one listing. Returns (products, pages fetched, html of page 1)."""

    products: list[dict] = []
    seen: set[str] = set()
    page = 1
    param = page_param
    first_html = ""
    while page <= MAX_PAGES_PER_LISTING:
        listing = listing_url if page == 1 or not param else with_page(listing_url, param, page)
        if page == 1 and seed_html is not None:
            html = seed_html
        else:
            html = (await fetcher.get(listing, origin=base)).html
        if page == 1:
            first_html = html
            if not param:
                param = detect_page_param(html, listing_url)
        found = extract_links(html, spec, listing)
        new = 0
        for item in found:
            url = item["url"] or ""
            if url in seen:
                continue
            seen.add(url)
            products.append(item)
            new += 1
        if not found or new == 0 or not param:
            break
        page += 1
    return products, page, first_html
