"""Collect product URLs of a site with the product regex only (no category regex).

A bounded breadth-first walk over the site's content pages: every page that is
not a product page is downloaded (up to ``max_listing_pages``), its product
links are recorded and its other internal links are queued.  Pages whose URL
looks like the listings where products were seen during exploration are
visited first, so the budget is spent inside the catalog rather than on
service pages.  Listings with a pager are walked page by page while they keep
yielding new products.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from app.agents.pattern import ProductPattern
from app.agents.shapes import tokenize
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher, Page
from app.clients.http.urls import origin
from app.parsers.links import extract_links, is_content_path, iter_anchors, matches
from app.parsers.paginate import detect_page_param
from app.parsers.paginate import page_url as with_page

log = logging.getLogger(__name__)

MIN_HITS_FOR_PAGER = 2


@dataclass(slots=True)
class CollectResult:
    products: dict[str, str] = field(default_factory=dict)  # url -> name from the listing
    pages_visited: int = 0  # pages downloaded by the collector (exploration pages excluded)
    failures: int = 0


def _key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", "")).lower()


def _without_query(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", "", ""))


def _path(url: str) -> str:
    return urlparse(url).path.rstrip("/") or "/"


class ProductCollector:
    def __init__(
        self,
        site_url: str,
        pattern: ProductPattern,
        fetcher: HtmlFetcher,
        *,
        max_listing_pages: int,
        max_pages_per_listing: int,
        max_products: int,
    ) -> None:
        self._site = origin(site_url)
        self._pattern = pattern
        self._re = pattern.compiled()
        self._fetcher = fetcher
        self._max_pages = max_listing_pages
        self._max_pager = max_pages_per_listing
        self._max_products = max_products
        self._result = CollectResult()
        self._visited: set[str] = set()
        self._queued: set[str] = set()
        self._hot: deque[str] = deque()
        self._cold: deque[str] = deque()
        self._hot_templates: set[tuple[str, ...]] = set()
        self._hot_prefixes: list[str] = []

    async def run(self, seed_pages: list[Page], listing_pages: list[str]) -> CollectResult:
        for url in listing_pages:
            path = _path(url)
            self._hot_templates.add(tokenize(path))
            parent = path.rsplit("/", 1)[0]
            if parent and parent != "/" and parent not in self._hot_prefixes:
                self._hot_prefixes.append(parent)
        # pages already downloaded during exploration are ingested without another request
        for page in seed_pages:
            self._visited.add(_key(page.url))
        for page in seed_pages:
            if self._re.search(_path(page.url)):
                continue
            await self._ingest(page)
            if self._done():
                break

        while not self._done():
            url = self._pop()
            if url is None:
                break
            try:
                page = await self._fetcher.get(url, origin=self._site)
            except HtmlFetchError as exc:
                self._result.failures += 1
                log.info("collect %s: skip %s (%s)", self._site, url, exc)
                continue
            self._result.pages_visited += 1
            self._visited.add(_key(page.url))
            await self._ingest(page)
        log.info(
            "collect %s: %d products, %d pages visited, %d failures",
            self._site, len(self._result.products), self._result.pages_visited, self._result.failures,
        )
        return self._result

    # ------------------------------------------------------------------ internals

    def _done(self) -> bool:
        return (
            len(self._result.products) >= self._max_products
            or self._result.pages_visited >= self._max_pages
        )

    def _pop(self) -> str | None:
        while self._hot or self._cold:
            url = self._hot.popleft() if self._hot else self._cold.popleft()
            if _key(url) not in self._visited:
                self._visited.add(_key(url))
                return url
        return None

    def _is_hot(self, path: str) -> bool:
        if tokenize(path) in self._hot_templates:
            return True
        return any(path.startswith(prefix + "/") for prefix in self._hot_prefixes)

    def _record(self, items: list[dict[str, str]]) -> int:
        new = 0
        for item in items:
            url = item["url"]
            if url in self._result.products:
                continue
            if len(self._result.products) >= self._max_products:
                break
            self._result.products[url] = item["name"]
            new += 1
        return new

    async def _ingest(self, page: Page) -> None:
        found = extract_links(page.html, self._re, page.url, self._pattern.name_mode)
        new = self._record(found)
        for anchor in iter_anchors(page.html, page.url):
            if matches(self._re, anchor) or not is_content_path(anchor.path):
                continue
            key = _key(anchor.url)
            if key in self._visited or key in self._queued:
                continue
            self._queued.add(key)
            (self._hot if self._is_hot(anchor.path) else self._cold).append(_without_query(anchor.url))
        if len(found) >= MIN_HITS_FOR_PAGER and new > 0 and not self._done():
            await self._paginate(page)

    async def _paginate(self, first: Page) -> None:
        listing = first.url
        if urlparse(listing).query:
            return  # already a pager page or a filtered view; the base page walks the pager
        param = self._pattern.page_param or detect_page_param(first.html, listing)
        if not param:
            return
        for number in range(2, self._max_pager + 1):
            if self._done():
                return
            url = with_page(listing, param, number)
            try:
                page = await self._fetcher.get(url, origin=self._site)
            except HtmlFetchError as exc:
                self._result.failures += 1
                log.info("collect %s: pager stops at %s (%s)", self._site, url, exc)
                return
            self._result.pages_visited += 1
            found = extract_links(page.html, self._re, page.url, self._pattern.name_mode)
            if not found or self._record(found) == 0:
                return
