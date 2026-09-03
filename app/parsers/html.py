from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from app.clients.http.fetch import HtmlFetcher
from app.clients.http.urls import abs_url, origin
from app.parsers.links import extract_links
from app.parsers.paginate import page_url as with_page
from app.parsers.schema import LinksSpec, SiteInstruction

MAX_LISTINGS = 80
MAX_PAGES = 50
PAGE_PARAM_RE = re.compile(r"[?&](page|p|pg)=\d+", re.I)


class ParseError(RuntimeError):
    pass


async def parse_site(site_url: str, instruction: SiteInstruction, fetcher: HtmlFetcher) -> dict:
    if (instruction.engine or "html").lower() != "html":
        raise ParseError(f"unsupported engine {instruction.engine!r}")
    base = origin(site_url)
    listing = abs_url(base, instruction.url or "/")
    declared = instruction.pagination.param if instruction.pagination else None
    first = await fetcher.get(listing, origin=base)
    listings = [listing, *_sublistings(first.html, listing, base, instruction.links)]
    products: list[dict] = []
    seen: set[str] = set()
    pages = 0
    for index, list_url in enumerate(listings):
        seed = first.html if index == 0 else None
        chunk, n = await _paginate(
            fetcher, list_url, base, instruction.links, declared, seed_html=seed
        )
        pages += n
        for item in chunk:
            url = item["url"] or ""
            if url in seen:
                continue
            seen.add(url)
            products.append(item)
    if not products:
        raise ParseError(f"no product links on {listing}")
    return {
        "site": base,
        "listing": listing,
        "listings": len(listings),
        "pages": pages,
        "total_products": len(products),
        "products": products,
    }


def _path(url: str) -> str:
    return urlparse(url).path.rstrip("/")


def _without_query(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _sublistings(html: str, listing_url: str, base: str, spec: LinksSpec) -> list[str]:
    root = _path(listing_url)
    if not root:
        return []
    prefix = root + "/"
    product_re = re.compile(spec.href, re.I) if spec.href else None
    found: list[str] = []
    seen = {_without_query(listing_url)}
    for item in extract_links(html, LinksSpec(), base):
        url = item["url"] or ""
        path = _path(url)
        if not path.startswith(prefix):
            continue
        if product_re and (product_re.search(path) or product_re.search(url)):
            continue
        key = _without_query(url)
        if key in seen:
            continue
        seen.add(key)
        found.append(key)
        if len(found) >= MAX_LISTINGS:
            break
    return found


def _infer_page_param(html: str) -> str | None:
    match = PAGE_PARAM_RE.search(html or "")
    return match.group(1) if match else None


async def _paginate(
    fetcher: HtmlFetcher,
    listing_url: str,
    base: str,
    spec: LinksSpec,
    page_param: str | None,
    seed_html: str | None = None,
) -> tuple[list[dict], int]:
    products: list[dict] = []
    seen: set[str] = set()
    page = 1
    param = page_param
    while page <= MAX_PAGES:
        listing = listing_url if page == 1 or not param else with_page(listing_url, param, page)
        if page == 1 and seed_html is not None:
            html = seed_html
        else:
            html = (await fetcher.get(listing, origin=base)).html
        if not param:
            param = _infer_page_param(html)
        found = extract_links(html, spec, base)
        new = 0
        for item in found:
            url = item["url"] or ""
            if url in seen:
                continue
            seen.add(url)
            products.append(item)
            new += 1
        if not found or new == 0:
            break
        if not param:
            break
        page += 1
    return products, page
