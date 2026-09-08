from __future__ import annotations

import re
from collections import Counter
from urllib.parse import parse_qsl, urlparse

from app.parsers.links import iter_anchors

PAGE_PARAM_NAMES = {
    "page", "p", "pg", "pagenum", "page_num", "pagenumber", "page_number", "pageno",
    "page_no", "pageindex", "page_index", "pagina", "seite", "start", "offset", "skip",
}
PAGE_PARAM_RE = re.compile(r"^(?:pagen_\d+|page\d*|p|pg)$", re.I)
INLINE_PAGE_RE = re.compile(r"[?&](page|p|pg|PAGEN_\d+)=\d+", re.I)


def page_url(url: str, param: str, page: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{param}={page}"


def detect_page_param(html: str, listing_url: str) -> str | None:
    """Query parameter used by the pager on this listing page, if any.

    Looks for links that point back to the same path with ``?param=<digits>``.
    """

    listing_path = urlparse(listing_url).path.rstrip("/") or "/"
    counts: Counter[str] = Counter()
    for anchor in iter_anchors(html, listing_url):
        if anchor.path != listing_path:
            continue
        query = urlparse(anchor.url).query
        if not query:
            continue
        for key, value in parse_qsl(query, keep_blank_values=False):
            if not value.isdigit():
                continue
            if key.lower() in PAGE_PARAM_NAMES or PAGE_PARAM_RE.match(key):
                counts[key] += 1
    if counts:
        return counts.most_common(1)[0][0]
    match = INLINE_PAGE_RE.search(html or "")
    return match.group(1) if match else None
