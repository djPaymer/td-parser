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
from app.clients.http.fetch import Fetcher, HtmlFetchError, Page
from app.clients.http.urls import origin, same_host
from app.parsers.links import foreign_hosts, is_content_path, iter_anchors

log = logging.getLogger(__name__)

MAX_CATALOG_ENTRIES = 2
MIN_SHAPE_COUNT = 2
CONFIRM_TOP_SHAPES = 2


@dataclass(slots=True)
class Crawl:
    site_url: str  # origin the catalog actually lives on (may differ from the requested URL)
    pages: list[Page]
    index: LinkIndex
    shapes: list[Shape]
    failures: list[str] = field(default_factory=list)
    entry_url: str = ""  # the URL that was requested
    moved_reason: str = ""  # why site_url differs from entry_url ("redirect" / "landing page links to ...")


MIN_OWN_LINKS = 5  # fewer same-host content links than this makes the home page a landing page
MIN_FOREIGN_LINKS = 10  # and this many links to one other host means the site lives there


def _key(url: str) -> str:
    return url.rstrip("/").lower()


class Explorer:
    def __init__(self, fetcher: Fetcher, budget: int) -> None:
        self._fetcher = fetcher
        self._budget = max(1, budget)

    async def run(self, site_url: str) -> Crawl:
        entry_url = site_url
        home = await self._fetcher.get(site_url, origin=site_url)
        site_url, home, moved_reason = await self._settle_origin(site_url, home)
        index = LinkIndex(site_url)
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
        return Crawl(
            site_url=site_url, pages=pages, index=index, shapes=shapes, failures=failures,
            entry_url=entry_url, moved_reason=moved_reason,
        )

    async def _settle_origin(self, site_url: str, home: Page) -> tuple[str, Page, str]:
        """Follow the site to the domain its catalog lives on.

        Two cases: the home page redirected to another host, or the home page is
        a landing page whose links (almost) all point at one other host.
        """

        if not same_host(home.url, site_url):
            target = origin(home.url)
            log.info("explore %s: redirected to %s", site_url, target)
            return target, home, f"redirected to {target}"
        own = sum(1 for a in iter_anchors(home.html, home.url) if is_content_path(a.path))
        if own >= MIN_OWN_LINKS:
            return site_url, home, ""
        foreign = foreign_hosts(home.html, home.url).most_common(1)
        if not foreign or foreign[0][1] < MIN_FOREIGN_LINKS:
            return site_url, home, ""
        host, count = foreign[0]
        target = f"https://{host}"
        try:
            moved_home = await self._fetcher.get(target, origin=target)
        except HtmlFetchError as exc:
            log.info("explore %s: landing page links to %s but it failed: %s", site_url, host, exc)
            return site_url, home, ""
        target = origin(moved_home.url)
        log.info("explore %s: landing page, %d of its links go to %s; continuing there", site_url, count, target)
        return target, moved_home, f"landing page links to {target}"


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
