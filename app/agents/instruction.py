"""Build a scrape instruction for a manufacturer site.

Pipeline:

1. ``Explorer`` downloads a budgeted sample of the site and groups every internal
   link into URL shapes (``app.agents.shapes``).
2. The LLM is shown the top shapes with statistics and examples and only has to
   *choose* which shape(s) are product-detail pages.  If the LLM is not
   configured or fails, a heuristic ranking makes the choice instead.
3. Code (not the LLM) turns the choice into an instruction: product regex,
   catalog entry URL, category regex for descending, pagination parameter.
4. The instruction is validated against the downloaded pages; a choice that
   matches too few links or looks like navigation is rejected and the next
   candidate is tried.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.agents.crawl import Crawl, Explorer
from app.agents.llm import LlmClient, LlmError
from app.agents.shapes import (
    HEX,
    NUM,
    WILD,
    Shape,
    expand_sections,
    shape_by_id,
    structural_siblings,
    template_text,
    templates_regex,
    tokenize,
)
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher, Page
from app.clients.http.urls import abs_url, origin
from app.core.config import AgentConfig
from app.parsers.links import compile_regex, extract_links
from app.parsers.paginate import detect_page_param
from app.parsers.schema import CategoriesSpec, LinksSpec, PaginationSpec, SiteInstruction

log = logging.getLogger(__name__)

MIN_LINKS_FOR_ANALYSIS = 5
MIN_LISTING_HITS = 2
MAX_NAV_RATIO = 0.5

CHOOSER_INSTRUCTIONS = (
    "You analyse the URL structure of a manufacturer's website to locate PRODUCT DETAIL pages "
    "(one page = one product, model or SKU).\n"
    "You get URL shapes ({*} = any path segment, {N} = digits) with statistics and sample links:\n"
    "- urls: distinct links of that shape seen so far\n"
    "- img: share of links that wrap an image (product cards usually do)\n"
    "- text / avg_text_len: anchor text presence and length (product names are longer than menu items)\n"
    "- nav: share of links present on most pages (site navigation = categories, never products)\n"
    "- visited / leaf: pages of this shape that were downloaded, and whether they had no deeper sub-pages "
    "(product pages are leaves; category pages are not)\n"
    "Pick the shape(s) whose links are individual products. If the catalog is split into several "
    "sections that share the same structure, include every section. Never pick category, series, "
    "brand, news or article shapes, and do not pick a shape just because it is the largest.\n"
    'Reply with JSON only: {"product_shapes": [ids], "confidence": 0.0-1.0, "reason": "one sentence"}. '
    "Use an empty list if no shape looks like product pages."
)


class AgentError(RuntimeError):
    pass


class _Rejected(RuntimeError):
    pass


@dataclass(slots=True)
class Choice:
    ids: list[int]
    source: str  # "llm" | "heuristic"
    confidence: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class BuildResult:
    instruction: SiteInstruction
    meta: dict[str, Any] = field(default_factory=dict)


class InstructionAgent:
    def __init__(self, config: AgentConfig, llm: LlmClient | None = None) -> None:
        self._config = config
        self._llm = llm

    @property
    def has_llm(self) -> bool:
        return self._llm is not None

    async def build_instruction(self, site_url: str, fetcher: HtmlFetcher) -> BuildResult:
        crawl = await Explorer(fetcher, self._config.max_pages).run(site_url)
        links_seen = len(crawl.index.links)
        if links_seen < MIN_LINKS_FOR_ANALYSIS:
            raise AgentError(
                f"{site_url}: only {links_seen} internal links found in HTML; "
                "the site is probably rendered by JavaScript or blocks bots"
            )
        candidates = [s for s in crawl.shapes if not s.literal and s.count >= 2]
        candidates = candidates[: self._config.max_candidates]
        if not candidates:
            raise AgentError(
                f"{site_url}: no repeating URL shapes among {links_seen} links "
                f"(pages: {', '.join(p.url for p in crawl.pages)})"
            )

        choices: list[Choice] = []
        llm_error = ""
        if self._llm is not None:
            try:
                choices.append(await self._ask_llm(crawl, candidates))
            except LlmError as exc:
                llm_error = str(exc)
                log.warning("instruction %s: LLM failed, using heuristics: %s", site_url, exc)
        choices.append(heuristic_choice(candidates))

        errors: list[str] = []
        tried: set[tuple[int, ...]] = set()
        for choice in choices:
            key = tuple(sorted(choice.ids))
            if not key or key in tried:
                continue
            tried.add(key)
            try:
                result = await self._assemble(crawl, candidates, choice, fetcher)
            except _Rejected as exc:
                errors.append(f"{choice.source} {list(key)}: {exc}")
                log.info("instruction %s: rejected %s choice %s: %s", site_url, choice.source, key, exc)
                continue
            result.meta["llm_error"] = llm_error or None
            result.meta["rejected"] = errors
            log.info("instruction %s: %s", site_url, result.instruction.model_dump(exclude_none=True))
            return result

        shapes_seen = "; ".join(f"#{s.id} {s.text} ({s.stats_line()})" for s in candidates[:6])
        hint = ""
        inner = crawl.index.pages[1:]
        if inner and sum(1 for p in inner if p.new_links == 0) >= len(inner) * 0.7:
            hint = (
                " Hint: category pages expose no links beyond the site menu, "
                "so the catalog is most likely rendered by JavaScript."
            )
        raise AgentError(
            f"{site_url}: could not build an instruction. "
            + (" | ".join(errors) if errors else "no shape was chosen.")
            + f" Shapes seen: {shapes_seen}."
            + hint
        )

    # ------------------------------------------------------------------ LLM

    async def _ask_llm(self, crawl: Crawl, candidates: list[Shape]) -> Choice:
        assert self._llm is not None
        payload = await self._llm.json_object(CHOOSER_INSTRUCTIONS, format_candidates(crawl, candidates))
        raw_ids = payload.get("product_shapes") or payload.get("shapes") or []
        if isinstance(raw_ids, (int, str)):
            raw_ids = [raw_ids]
        valid = {s.id for s in candidates}
        ids: list[int] = []
        for value in raw_ids if isinstance(raw_ids, list) else []:
            try:
                number = int(str(value).lstrip("#"))
            except ValueError:
                continue
            if number in valid and number not in ids:
                ids.append(number)
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(payload.get("reason") or "")[:300]
        log.info("instruction %s: LLM chose %s (%.2f) %s", crawl.site_url, ids, confidence, reason)
        return Choice(ids=ids, source="llm", confidence=max(0.0, min(confidence, 1.0)), reason=reason)

    # ------------------------------------------------------------------ assembly

    async def _assemble(
        self,
        crawl: Crawl,
        candidates: list[Shape],
        choice: Choice,
        fetcher: HtmlFetcher,
    ) -> BuildResult:
        chosen = shape_by_id(candidates, choice.ids)
        if not chosen:
            raise _Rejected("no valid shape ids")
        if choice.source == "heuristic":
            # the LLM saw every candidate and picked deliberately; the heuristic picks one
            for shape in list(chosen):
                for sibling in structural_siblings(candidates, shape):
                    if sibling not in chosen:
                        chosen.append(sibling)
        # drop shapes the crawl has proven to be navigation or category pages
        dropped: list[str] = []
        for shape in list(chosen):
            if shape.nav_ratio > MAX_NAV_RATIO:
                dropped.append(f"{shape.text} is site navigation (nav={shape.nav_ratio:.0%})")
            elif shape.leaf is False:
                dropped.append(f"{shape.text} pages have sub-pages, so they are categories")
            else:
                continue
            chosen.remove(shape)
        if not chosen:
            raise _Rejected("; ".join(dropped))
        if dropped:
            log.info("instruction %s: dropped from %s choice: %s", crawl.site_url, choice.source, "; ".join(dropped))

        templates = expand_sections([s.template for s in chosen], crawl.shapes)
        spec = LinksSpec(href=templates_regex(templates), name=name_mode(chosen))
        product_re = compile_regex(spec.href, "links.href")
        assert product_re is not None

        hits_by_page: list[tuple[Page, int]] = []
        distinct: set[str] = set()
        for page in crawl.pages:
            found = extract_links(page.html, spec, page.url)
            distinct.update(item["url"] or "" for item in found)
            hits_by_page.append((page, len(found)))
        if len(distinct) < self._config.min_hits:
            raise _Rejected(f"matched only {len(distinct)} product links on {len(crawl.pages)} pages")
        # pages that list products; product pages themselves (related items) and pages with a
        # single incidental product link do not count
        listing_pages = sorted(
            [(p, n) for p, n in hits_by_page if n >= MIN_LISTING_HITS and not product_re.search(_path_of(p.url))],
            key=lambda pn: -pn[1],
        )
        if not listing_pages:
            raise _Rejected("product links were seen only on product pages")

        root = await self._pick_root(crawl, templates, listing_pages, fetcher)
        if product_re.search(root):
            raise _Rejected(f"product regex matches the catalog entry {root}")
        categories = category_regex(root, templates, listing_pages)
        pagination = None
        for page, _ in listing_pages[:3]:
            param = detect_page_param(page.html, page.url)
            if param:
                pagination = PaginationSpec(param=param)
                break

        instruction = SiteInstruction(
            engine="html",
            url=root,
            links=spec,
            categories=CategoriesSpec(href=categories) if categories else None,
            pagination=pagination,
        )
        meta = {
            "source": choice.source,
            "confidence": choice.confidence,
            "reason": choice.reason,
            "shapes": [s.text for s in chosen],
            "dropped": dropped,
            "hits": len(distinct),
            "listing_pages": [{"url": p.url, "hits": n} for p, n in listing_pages[:10]],
            "pages_fetched": len(crawl.pages),
            "links_seen": len(crawl.index.links),
            "fetch_failures": crawl.failures,
            "candidates": [
                {"id": s.id, "shape": s.text, "score": s.score, "urls": s.count, "leaf": s.leaf}
                for s in candidates
            ],
        }
        return BuildResult(instruction=instruction, meta=meta)

    async def _pick_root(
        self,
        crawl: Crawl,
        product_templates: list[tuple[str, ...]],
        listing_pages: list[tuple[Page, int]],
        fetcher: HtmlFetcher,
    ) -> str:
        """Catalog entry path.

        Preferably the static prefix shared by the pages where products were actually
        listed (``/products``), else the prefix of the product URLs themselves
        (``/product`` for ``/product/{N}``) if that is a real page, else the home page.
        """

        fetched = {_path_of(p.url) for p in crawl.pages}
        failed = {_path_of(u) for u in crawl.failures}
        listing_templates = [tokenize(_path_of(p.url)) for p, _ in listing_pages]
        for path in (literal_prefix(listing_templates), literal_prefix(product_templates)):
            if not path or path == "/":
                continue
            if path in fetched:
                return path
            if path in failed:
                continue
            try:
                page = await fetcher.get(abs_url(origin(crawl.site_url), path), origin=crawl.site_url)
            except HtmlFetchError:
                crawl.failures.append(path)
                continue
            crawl.pages.append(page)
            crawl.index.add_page(page)
            return path
        return "/"


# ---------------------------------------------------------------------- helpers


def heuristic_choice(candidates: list[Shape]) -> Choice:
    top = candidates[0]
    runner = candidates[1].score if len(candidates) > 1 else top.score - 3
    confidence = max(0.2, min(0.8, 0.4 + (top.score - runner) / 6))
    return Choice(ids=[top.id], source="heuristic", confidence=round(confidence, 2), reason="best heuristic score")


def name_mode(shapes: list[Shape]) -> str:
    total = sum(s.count for s in shapes) or 1
    text = sum(s.text_ratio * s.count for s in shapes) / total
    title = sum(s.title_ratio * s.count for s in shapes) / total
    if text < 0.5 <= title and title > text:
        return "title"
    return "text"


def literal_prefix(templates: list[tuple[str, ...]]) -> str:
    """Common leading literal segments of the templates (``/product/{N}`` -> ``/product``)."""

    if not templates:
        return "/"
    prefix: list[str] = []
    for pos in range(min(len(t) for t in templates)):
        tokens = {t[pos] for t in templates}
        if len(tokens) != 1:
            break
        token = next(iter(tokens))
        if WILD in token or NUM in token or HEX in token:
            break
        prefix.append(token)
    return template_text(tuple(prefix))


def category_regex(
    root: str,
    product_templates: list[tuple[str, ...]],
    listing_pages: list[tuple[Page, int]],
) -> str | None:
    """Regex for category pages below ``root`` that the parser may follow.

    Covers the listing pages where products were actually seen, every
    intermediate level between ``root`` and those pages, and every intermediate
    level between ``root`` and the product templates.
    """

    root_tpl = tuple(s for s in root.split("/") if s)
    depth = len(root_tpl)
    found: set[tuple[str, ...]] = set()
    for page, _ in listing_pages:
        tpl = tokenize(_path_of(page.url))
        for k in range(depth + 1, len(tpl) + 1):
            found.add(tpl[:k])
    for tpl in product_templates:
        for k in range(depth + 1, len(tpl)):
            found.add(tpl[:k])
    found.discard(root_tpl)
    if depth:
        found = {t for t in found if t[:depth] == root_tpl}
    if not found:
        return None
    return templates_regex(sorted(found, key=lambda t: (len(t), t)))


def format_candidates(crawl: Crawl, candidates: list[Shape]) -> str:
    lines = [
        f"Website: {crawl.site_url}",
        "Downloaded pages: " + ", ".join(_path_of(p.url) for p in crawl.pages),
        "",
        "URL shapes (most promising first):",
    ]
    for shape in candidates:
        lines.append(f"#{shape.id} {shape.text}")
        lines.append(f"   {shape.stats_line()}")
        for path, name in shape.examples(3):
            name = re.sub(r"\s+", " ", name)[:70]
            lines.append(f"   {path}  ->  \"{name}\"")
    lines.append("")
    lines.append("Which shape ids are product detail pages?")
    return "\n".join(lines)


def _path_of(url: str) -> str:
    return urlparse(url).path.rstrip("/") or "/"
