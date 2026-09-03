from __future__ import annotations

import re
from urllib.parse import urlparse

from app.clients.http.fetch import Page
from app.clients.http.urls import origin, same_host
from app.parsers.links import extract_links
from app.parsers.schema import LinksSpec, SiteInstruction

CATALOG_RE = re.compile(
    r"product|products|mall|catalog|shop|goods|category|prolist|store|katalog",
    re.I,
)
SKIP_PATH_RE = re.compile(
    r"news|blog|about|contact|inquiry|career|login|cart|privacy|"
    r"cdn-cgi|email-protection|facebook|instagram|linkedin|youtube",
    re.I,
)


def page_links(html: str, base_url: str) -> list[dict[str, str | None]]:
    return extract_links(html or "", LinksSpec(), base_url)


def path_of(url: str) -> str:
    path = urlparse(url or "").path.rstrip("/") or "/"
    if path != "/" and not path.startswith("/"):
        path = "/" + path
    return path


def path_template(path: str) -> str:
    return re.sub(r"\d+", "N", path or "/")


def catalog_candidate_urls(links: list[dict[str, str | None]], base: str, limit: int) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    home = origin(base).rstrip("/")
    for item in links:
        url = (item.get("url") or "").rstrip("/")
        if not url or url == home or not same_host(base, url):
            continue
        path = path_of(url)
        if path in seen or path == "/":
            continue
        if SKIP_PATH_RE.search(path) or not CATALOG_RE.search(path):
            continue
        seen.add(path)
        scored.append((path.count("/"), len(path), url))
    scored.sort()
    return [url for _, _, url in scored[:limit]]


MAX_EXAMPLES_PER_SHAPE = 3
MAX_LINK_ROWS = 60


def format_outline(site_url: str, pages: list[Page], limit: int) -> str:
    lines = [
        f"Manufacturer website: {site_url}",
        "Fetched pages:",
        *[f"- {page.url}" for page in pages],
        "",
        "Write a SHORT regex for a product-detail path shape. Do not list URLs.",
        "",
    ]
    all_links: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for page in pages:
        for item in page_links(page.html, page.url or site_url):
            path = path_of(item.get("url") or "")
            if path in seen:
                continue
            seen.add(path)
            all_links.append(item)
    grouped: dict[str, list[dict[str, str | None]]] = {}
    for item in all_links:
        grouped.setdefault(path_template(path_of(item.get("url") or "")), []).append(item)
    ranked = sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    lines.append("Path shapes (N = digits). Pick a PRODUCT detail shape, not index/category:")
    for pattern, items in ranked[:20]:
        lines.append(f"{len(items):4}  {pattern}")
    lines.append("")
    lines.append("Examples (a few per shape):")
    rows = 0
    for _, items in ranked:
        for item in items[:MAX_EXAMPLES_PER_SHAPE]:
            path = path_of(item.get("url") or "")
            name = (item.get("name") or "").replace("\n", " ").strip()
            if len(name) > 60:
                name = name[:57] + "..."
            lines.append(f"{path}  →  {name}".rstrip())
            rows += 1
            if rows >= MAX_LINK_ROWS:
                break
        if rows >= MAX_LINK_ROWS:
            break
    text = "\n".join(lines)
    if limit and len(text) > limit:
        return text[:limit]
    return text


def count_hits(instruction: SiteInstruction, pages: list[Page], base: str) -> tuple[int, int]:
    if not instruction.links.href:
        return 0, 0
    href_only = LinksSpec(href=instruction.links.href, name="text")
    with_spec = 0
    href_hits = 0
    for page in pages:
        html = page.html or ""
        origin_url = page.url or base
        with_spec += len(extract_links(html, instruction.links, origin_url))
        href_hits += len(extract_links(html, href_only, origin_url))
    return with_spec, href_hits


def sample_paths(pages: list[Page], limit: int = 30) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for page in pages:
        for item in page_links(page.html, page.url or ""):
            path = path_of(item.get("url") or "")
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
            if len(out) >= limit:
                return out
    return out


def retry_hint(
    instruction: SiteInstruction,
    pages: list[Page],
    hits: int,
    href_hits: int,
) -> str:
    samples = "\n".join(sample_paths(pages, 40))
    extra = ""
    if hits == 0 and href_hits > 0:
        extra = "Omit links.class; it filtered out every match.\n"
    return (
        "Previous instruction matched 0 product links on the fetched pages.\n"
        f"Previous JSON: {instruction.model_dump(by_alias=True, exclude_none=True)}\n"
        f"{extra}"
        "href MUST be a short regex (under 80 chars) for a PRODUCT detail path shape. "
        "Do not invent goods-\\d+.html, /product/\\d+, or prolist_t unless those shapes appear. "
        "Do not paste a list of URLs.\n"
        f"Paths:\n{samples}"
    )
