"""Budgeted, shape-driven exploration of a manufacturer site.

Instead of guessing catalog pages by English keywords, the crawler repeatedly
looks at the URL shapes discovered so far and visits one representative of
every shape that has not been visited yet, largest shape first.  That walks a
catalog hierarchy top-down (sections -> categories -> products) regardless of
language, and tells us which shapes are leaves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.agents.shapes import LinkIndex, Shape, build_shapes, catalog_entry_links
from app.clients.http.fetch import HtmlFetcher, HtmlFetchError, Page

log = logging.getLogger(__name__)

MAX_CATALOG_ENTRIES = 2
MIN_SHAPE_COUNT = 2
CONFIRM_TOP_SHAPES = 2


@dataclass(slots=True)
class Crawl:
    site_url: str
    pages: list[Page]
    index: LinkIndex
    shapes: list[Shape]
    failures: list[str] = field(default_factory=list)


def _key(url: str) -> str:
    return url.rstrip("/").lower()


class Explorer:
    def __init__(self, fetcher: HtmlFetcher, budget: int) -> None:
        self._fetcher = fetcher
        self._budget = max(1, budget)

    async def run(self, site_url: str) -> Crawl:
        index = LinkIndex(site_url)
        home = await self._fetcher.get(site_url, origin=site_url)
        pages = [home]
        index.add_page(home)
        visited = {_key(home.url), _key(site_url)}
        failures: list[str] = []

        async def visit(url: str) -> bool:
            if _key(url) in visited or len(pages) >= self._budget:
                return False
            visited.add(_key(url))
            try:
                page = await self._fetcher.get(url, origin=site_url)
            except HtmlFetchError as exc:
                log.info("explore: skip %s (%s)", url, exc)
                failures.append(url)
                return False
            visited.add(_key(page.url))
            pages.append(page)
            index.add_page(page)
            return True

        # 1. obvious catalog entry pages (/products, /catalog, ...)
        for info in catalog_entry_links(index)[:MAX_CATALOG_ENTRIES]:
            await visit(info.url)

        # 2. descend: one representative of every unexplored shape, largest first
        while len(pages) < self._budget:
            shapes = build_shapes(index)
            target = _next_target(shapes, visited)
            if target is None:
                break
            await visit(target)

        # 3. confirm the most promising shapes with a second sample
        shapes = build_shapes(index)
        for shape in [s for s in shapes if not s.literal][:CONFIRM_TOP_SHAPES]:
            if len(pages) >= self._budget:
                break
            if len(shape.explored) >= 2:
                continue
            target = _representative(shape, visited)
            if target:
                await visit(target)

        shapes = build_shapes(index)
        log.info(
            "explore %s: %d pages, %d links, %d shapes, %d failures",
            site_url, len(pages), len(index.links), len(shapes), len(failures),
        )
        return Crawl(site_url=site_url, pages=pages, index=index, shapes=shapes, failures=failures)


def _representative(shape: Shape, visited: set[str]) -> str | None:
    ranked = sorted(shape.links, key=lambda l: (-len(l.pages), len(l.path)))
    for link in ranked:
        if _key(link.url) not in visited:
            return link.url
    return None


def _next_target(shapes: list[Shape], visited: set[str]) -> str | None:
    """Deepest unexplored shape first: products are the leaves, so descend as fast as possible."""

    candidates = [
        s for s in shapes
        if not s.literal and not s.explored and s.count >= MIN_SHAPE_COUNT
    ]
    candidates.sort(key=lambda s: (-s.depth, -s.count))
    for shape in candidates:
        target = _representative(shape, visited)
        if target:
            return target
    return None
