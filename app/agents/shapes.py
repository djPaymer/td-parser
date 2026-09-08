"""URL shape analysis.

Groups the internal links of a site into *shapes* (path templates such as
``/product/{N}`` or ``/{*}/{*}/{*}``), computes statistics that separate
product-detail pages from category / navigation pages, and synthesises the
regex for a chosen set of shapes.  Everything here is deterministic; the LLM
only has to pick between the candidates this module produces.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from app.clients.http.fetch import Page
from app.parsers.links import Anchor, is_content_path, iter_anchors

WILD = "{*}"
NUM = "{N}"
HEX = "{H}"

MIN_COLLAPSE = 3  # distinct sibling segments needed to generalise a position
FIRST_SEGMENT_COLLAPSE = 9  # the first segment is kept literal unless this many siblings
MAX_TEMPLATE_ALTERNATIVES = 12

EXT_RE = re.compile(r"\.(?:html?|php|aspx?|jsp|shtml|cfm)$", re.I)
HEX_RE = re.compile(
    r"^(?:[0-9a-f]{12,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I
)
PRODUCT_WORD_RE = re.compile(
    r"product|produkt|produit|producto|prodotto|item|goods|detail|sku|model|tovar|artikel|"
    r"^p$|^pd$|^dt_|^show|^view|^info", re.I
)
CATEGORY_WORD_RE = re.compile(
    r"categor|catalog|katalog|catalogue|collection|section|razdel|series|prolist|list|group|"
    r"brand|application|solution|industry|range", re.I
)
CATALOG_ENTRY_RE = re.compile(
    r"^(?:products?|catalog|catalogs|catalogue|katalog|shop|store|goods|mall|produkte|produits|"
    r"productos|prodotti|catalogo|tovary|assortment|range|collections?|items|prolist|product-center|"
    r"productcenter|product_center|cp|chanpin|our-products|all-products|product-list)$",
    re.I,
)


# --------------------------------------------------------------------------- tokens


def tokenize(path: str) -> tuple[str, ...]:
    """Split a path into generalised segments: digits -> {N}, long hex -> {H}."""

    out: list[str] = []
    for seg in path.split("/"):
        if not seg:
            continue
        seg = unquote(seg).lower()
        if HEX_RE.match(seg):
            out.append(HEX)
            continue
        out.append(re.sub(r"\d+", NUM, seg))
    return tuple(out)


def template_text(template: tuple[str, ...]) -> str:
    return "/" + "/".join(template) if template else "/"


def is_variable(token: str) -> bool:
    return token == WILD or token == HEX or NUM in token


def _token_regex(token: str) -> str:
    if token == WILD:
        return r"[^/?#]+"
    if token == HEX:
        return r"[0-9a-f-]+"
    parts = token.split(NUM)
    return r"\d+".join(re.escape(p) for p in parts)


def _position_regex(tokens: set[str]) -> str:
    if WILD in tokens or len(tokens) > MAX_TEMPLATE_ALTERNATIVES:
        return _token_regex(WILD)
    if len(tokens) == 1:
        return _token_regex(next(iter(tokens)))
    return "(?:" + "|".join(sorted(_token_regex(t) for t in tokens)) + ")"


def templates_regex(templates: list[tuple[str, ...]], *, prefix_depths: bool = False) -> str:
    """Anchored regex matching either a path or a full URL of the given templates.

    Templates of equal length are merged position-wise (``/(?:a|b)/[^/]+``).
    With ``prefix_depths`` the pattern also matches every strict prefix of the
    templates, which is what category regexes need.
    """

    by_len: dict[int, list[tuple[str, ...]]] = {}
    for tpl in templates:
        if tpl:
            by_len.setdefault(len(tpl), []).append(tpl)
    bodies: list[str] = []
    for length in sorted(by_len):
        group = by_len[length]
        segs = []
        for pos in range(length):
            segs.append(_position_regex({t[pos] for t in group}))
        if prefix_depths:
            body = "/" + segs[0]
            for seg in segs[1:]:
                body += "(?:/" + seg
            body += ")?" * (length - 1)
        else:
            body = "/" + "/".join(segs)
        bodies.append(body)
    if not bodies:
        return ""
    core = bodies[0] if len(bodies) == 1 else "(?:" + "|".join(bodies) + ")"
    return r"^(?:https?://[^/?#]+)?" + core + r"/?$"


# --------------------------------------------------------------------------- index


@dataclass(slots=True)
class LinkInfo:
    url: str
    path: str
    template: tuple[str, ...]
    texts: set[str] = field(default_factory=set)
    title: str = ""
    has_img: bool = False
    pages: set[str] = field(default_factory=set)
    first_page: str = ""


@dataclass(slots=True)
class PageInfo:
    url: str
    path: str
    template: tuple[str, ...]
    links: set[str]  # same-host content URLs found on the page
    new_links: int  # URLs first discovered on this page (crawl order)
    descendants: int  # links whose path is below this page's path
    deeper_new: int  # newly discovered links with a deeper path than the page (sub-items)


class LinkIndex:
    """All usable internal links seen on the fetched pages."""

    def __init__(self, site_url: str) -> None:
        self.site_url = site_url
        self.links: dict[str, LinkInfo] = {}
        self.pages: list[PageInfo] = []
        self.anchors: dict[str, list[Anchor]] = {}  # page url -> anchors (for pagination detection)

    @property
    def page_urls(self) -> set[str]:
        return {p.url for p in self.pages}

    def add_page(self, page: Page) -> PageInfo:
        page_url = page.url.rstrip("/") or page.url
        page_path = urlparse(page.url).path.rstrip("/") or "/"
        anchors = iter_anchors(page.html, page.url or self.site_url)
        self.anchors[page_url] = anchors
        found: set[str] = set()
        new_links = 0
        descendants = 0
        deeper_new = 0
        prefix = page_path + "/" if page_path != "/" else None
        page_depth = page_path.count("/") if page_path != "/" else 0
        for anchor in anchors:
            if not is_content_path(anchor.path):
                continue
            url = anchor.url
            info = self.links.get(url)
            is_new = info is None
            if info is None:
                info = LinkInfo(url=url, path=anchor.path, template=tokenize(anchor.path), first_page=page_url)
                self.links[url] = info
                new_links += 1
            if anchor.text:
                info.texts.add(anchor.text[:120])
            if anchor.title and not info.title:
                info.title = anchor.title[:120]
            info.has_img = info.has_img or anchor.has_img
            info.pages.add(page_url)
            if url not in found:
                found.add(url)
                if prefix and anchor.path.startswith(prefix):
                    descendants += 1
                if is_new and anchor.path.count("/") > page_depth:
                    deeper_new += 1
        info = PageInfo(
            url=page_url,
            path=page_path,
            template=tokenize(page_path),
            links=found,
            new_links=new_links,
            descendants=descendants,
            deeper_new=deeper_new,
        )
        self.pages.append(info)
        return info

    def links_of(self, page_url: str) -> list[LinkInfo]:
        page = next((p for p in self.pages if p.url == page_url), None)
        if page is None:
            return []
        return [self.links[u] for u in page.links if u in self.links]


# --------------------------------------------------------------------------- shapes


@dataclass(slots=True)
class Shape:
    id: int
    template: tuple[str, ...]
    links: list[LinkInfo]
    count: int = 0
    depth: int = 0
    img_ratio: float = 0.0
    text_ratio: float = 0.0
    title_ratio: float = 0.0
    avg_text_len: float = 0.0
    nav_ratio: float = 0.0
    explored: list[PageInfo] = field(default_factory=list)
    leaf: bool | None = None
    score: float = 0.0
    literal: bool = False

    @property
    def text(self) -> str:
        return template_text(self.template)

    def regex(self) -> str:
        return templates_regex([self.template])

    def examples(self, n: int = 3) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        ranked = sorted(self.links, key=lambda l: -max((len(t) for t in l.texts), default=0))
        for link in ranked[:n]:
            name = max(link.texts, key=len) if link.texts else (link.title or "")
            out.append((link.path, name))
        return out

    def stats_line(self) -> str:
        leaf = "?" if self.leaf is None else ("yes" if self.leaf else "no")
        return (
            f"urls={self.count} depth={self.depth} img={self.img_ratio:.0%} text={self.text_ratio:.0%} "
            f"avg_text_len={self.avg_text_len:.0f} nav={self.nav_ratio:.0%} "
            f"visited={len(self.explored)} leaf={leaf}"
        )


LOCALE_RE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.I)


def section_position(template: tuple[str, ...]) -> int:
    """Index of the segment that names the site section (0, or 1 after a locale like /en-us)."""

    return 1 if template and LOCALE_RE.match(template[0]) and len(template) > 1 else 0


def _collapse(templates: set[tuple[str, ...]]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Generalise a position when enough distinct siblings share the same prefix.

    Siblings are grouped by (length, prefix) only, so ``/product/drill/cdli{N}``
    and ``/product/grinder/cagli{N}`` collapse into ``/product/{*}/{*}`` although
    no two of them agree on the last segment.
    """

    current = {t: t for t in templates}
    if not current:
        return current
    max_len = max(len(t) for t in current)
    for pos in reversed(range(max_len)):
        groups: dict[tuple[int, tuple[str, ...]], set[str]] = {}
        for cur in set(current.values()):
            if not _collapsible(cur, pos):
                continue
            groups.setdefault((len(cur), cur[:pos]), set()).add(cur[pos])
        for orig, cur in list(current.items()):
            if not _collapsible(cur, pos):
                continue
            threshold = FIRST_SEGMENT_COLLAPSE if pos == section_position(cur) else MIN_COLLAPSE
            if len(groups.get((len(cur), cur[:pos]), ())) >= threshold:
                current[orig] = cur[:pos] + (WILD,) + cur[pos + 1 :]
    return current


def _collapsible(template: tuple[str, ...], pos: int) -> bool:
    if len(template) <= pos or template[pos] == WILD:
        return False
    section = section_position(template)
    if pos < section:
        return False  # never generalise a locale prefix
    # the section segment (/tools, /news, /en-us/products) is generalised only for flat sites
    # whose products live directly under it (/{slug}, /en/{slug})
    return pos > section or len(template) == section + 1


def _matches_template(general: tuple[str, ...], specific: tuple[str, ...]) -> bool:
    if len(general) != len(specific):
        return False
    return all(g == WILD or g == s for g, s in zip(general, specific))


def _absorb(mapping: dict[tuple[str, ...], tuple[str, ...]]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Fold leftover specific templates into the least general sibling that covers them."""

    general = sorted({g for g in mapping.values() if WILD in g}, key=lambda g: g.count(WILD))
    out = dict(mapping)
    for orig, cur in mapping.items():
        for g in general:
            if g != cur and g.count(WILD) > cur.count(WILD) and _matches_template(g, cur):
                out[orig] = g
                break
    return out


def _generalize_prefixes(mapping: dict[tuple[str, ...], tuple[str, ...]]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Rewrite ``/A/B/C/{*}/{*}`` as ``/A/{*}/{*}/{*}/{*}`` when ``/A/{*}/{*}`` is a known shape.

    A product's parent path is a category path; category shapes are usually
    well sampled (navigation), while products are seen only under the few
    categories that were downloaded.  Borrowing the parent's generalisation
    lets one product shape cover the whole section.
    """

    out = dict(mapping)
    changed = True
    while changed:
        changed = False
        by_len: dict[int, set[tuple[str, ...]]] = {}
        for tpl in set(out.values()):
            if WILD in tpl:
                by_len.setdefault(len(tpl), set()).add(tpl)
        for orig, cur in list(out.items()):
            keep = section_position(cur) + 1  # locale + section segments are never borrowed
            for k in range(len(cur) - 1, keep, -1):
                prefix = cur[:k]
                better = [
                    g for g in by_len.get(k, ())
                    if g != prefix and g[:keep] == prefix[:keep] and _matches_template(g, prefix)
                    and g.count(WILD) > prefix.count(WILD)
                ]
                if better:
                    best = min(better, key=lambda g: g.count(WILD))
                    out[orig] = best + cur[k:]
                    changed = True
                    break
    return out


def generalize_templates(templates: set[tuple[str, ...]]) -> dict[tuple[str, ...], tuple[str, ...]]:
    """Map every raw path template to its generalised shape.

    Collapse siblings, absorb leftovers, borrow parent generalisations; repeat
    because each step can enable the next (a product code under a category that
    was only generalised in the previous round now has enough siblings).
    """

    mapping = {t: t for t in templates}
    for _ in range(4):
        before = dict(mapping)
        step = _collapse(set(mapping.values()))
        mapping = {orig: step.get(cur, cur) for orig, cur in mapping.items()}
        mapping = _generalize_prefixes(_absorb(mapping))
        if mapping == before:
            break
    return mapping


def build_shapes(index: LinkIndex) -> list[Shape]:
    if not index.links:
        return []
    mapping = generalize_templates({info.template for info in index.links.values()})
    grouped: dict[tuple[str, ...], list[LinkInfo]] = {}
    for info in index.links.values():
        grouped.setdefault(mapping.get(info.template, info.template), []).append(info)
    pages_total = len(index.pages)
    nav_cutoff = max(2, math.ceil(pages_total * 2 / 3))
    shapes: list[Shape] = []
    for template, links in grouped.items():
        shape = Shape(id=0, template=template, links=links)
        shape.count = len(links)
        shape.depth = len(template)
        shape.img_ratio = sum(1 for l in links if l.has_img) / shape.count
        shape.text_ratio = sum(1 for l in links if l.texts) / shape.count
        shape.title_ratio = sum(1 for l in links if l.title) / shape.count
        lengths = [max(len(t) for t in l.texts) for l in links if l.texts]
        shape.avg_text_len = sum(lengths) / len(lengths) if lengths else 0.0
        if pages_total >= 3:
            shape.nav_ratio = sum(1 for l in links if len(l.pages) >= nav_cutoff) / shape.count
        shape.literal = not any(is_variable(tok) for tok in template)
        shape.explored = [p for p in index.pages if mapping.get(p.template, p.template) == template]
        if shape.explored:
            # a category page reveals sub-items: links below its own path, or previously unseen
            # links deeper than itself (listings often show products of sibling categories).
            # A product page reveals neither; its related products sit at the same depth.
            avg_desc = sum(p.descendants for p in shape.explored) / len(shape.explored)
            avg_deeper = sum(p.deeper_new for p in shape.explored) / len(shape.explored)
            shape.leaf = avg_desc < 3 and avg_deeper < 3
        shape.score = _score(shape)
        shapes.append(shape)
    shapes.sort(key=lambda s: (-s.score, -s.count, s.text))
    for i, shape in enumerate(shapes, start=1):
        shape.id = i
    return shapes


def _score(shape: Shape) -> float:
    if shape.literal:
        return -10.0
    text = shape.text
    last = shape.template[-1] if shape.template else ""
    score = math.log(shape.count + 1) * 1.2
    score += min(shape.depth, 4) * 0.35
    score += shape.img_ratio * 2.0
    if shape.avg_text_len >= 18:
        score += 1.0
    elif shape.avg_text_len and shape.avg_text_len < 8:
        score -= 0.5
    score -= shape.nav_ratio * 3.0
    if PRODUCT_WORD_RE.search(text):
        score += 1.5
    if CATEGORY_WORD_RE.search(text) and not PRODUCT_WORD_RE.search(text):
        score -= 1.0
    if NUM in last or HEX in last or EXT_RE.search(last):
        score += 0.5
    if shape.leaf is True:
        score += 1.5
    elif shape.leaf is False:
        score -= 2.5
    return round(score, 3)


def shape_by_id(shapes: list[Shape], ids: list[int]) -> list[Shape]:
    by_id = {s.id: s for s in shapes}
    return [by_id[i] for i in ids if i in by_id]


def structural_siblings(shapes: list[Shape], chosen: Shape) -> list[Shape]:
    """Shapes with the same wildcard structure that differ only in literal segments.

    Used by the heuristic fallback when a site is split into sections
    (``/tools/{*}/{*}/{*}``, ``/garden/{*}/{*}/{*}``); flat shapes (depth < 3) are
    not widened because ``/products/{*}`` and ``/product-category/{*}`` look alike.
    """

    out: list[Shape] = []
    if len(chosen.template) < section_position(chosen.template) + 3:
        return out
    for shape in shapes:
        if shape is chosen or shape.literal or len(shape.template) != len(chosen.template):
            continue
        if not same_structure(shape.template, chosen.template) or shape.leaf is False:
            continue
        if shape.score >= chosen.score * 0.6 and shape.count >= 2:
            out.append(shape)
    return out


def same_structure(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) == len(right) and all(
        (a == WILD) == (b == WILD) and (NUM in a) == (NUM in b) for a, b in zip(left, right)
    )


def expand_sections(templates: list[tuple[str, ...]], shapes: list[Shape]) -> list[tuple[str, ...]]:
    """Extend product templates to sibling site sections with the same hierarchy.

    Given product shapes ``/tools/{*}/{*}/{*}`` and ``/garden/{*}/{*}/{*}`` and a
    category shape ``/fasteners/{*}/{*}`` (same depth as the products' parent),
    the products of the third section are ``/fasteners/{*}/{*}/{*}`` even if no
    such link was downloaded.

    Conservative on purpose: it only generalises a multi-section pattern that
    was already chosen (two or more sections), only for hierarchies (depth >= 3),
    and never into a shape the crawl has shown to be navigation or a category.
    """

    out = set(templates)
    sections = {t[section_position(t)] for t in templates if len(t) > section_position(t)}
    if len(sections) < 2:
        return sorted(out)
    disproved = {s.template for s in shapes if s.leaf is False or s.nav_ratio > 0.5}
    for tpl in templates:
        sec = section_position(tpl)
        if len(tpl) < sec + 3 or is_variable(tpl[sec]):
            continue
        parent_len = len(tpl) - 1
        for shape in shapes:
            other = shape.template
            if len(other) < parent_len or section_position(other) != sec or other[:sec] != tpl[:sec]:
                continue
            if other[sec] == tpl[sec] or is_variable(other[sec]):
                continue
            candidate = tpl[:sec] + (other[sec],) + tpl[sec + 1 :]
            if candidate in disproved:
                continue
            if same_structure(other[sec + 1 : parent_len], tpl[sec + 1 : parent_len]):
                out.add(candidate)
    return sorted(out)


def catalog_entry_links(index: LinkIndex) -> list[LinkInfo]:
    """Single links that look like the catalog index page (``/products``, ``/catalog``...)."""

    out: list[LinkInfo] = []
    for info in index.links.values():
        segs = [s for s in info.path.split("/") if s]
        if segs and len(segs) <= 2 and CATALOG_ENTRY_RE.match(unquote(segs[-1])):
            out.append(info)
    out.sort(key=lambda l: (len(l.path.split("/")), -len(l.pages)))
    return out
