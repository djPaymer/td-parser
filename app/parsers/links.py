from __future__ import annotations

import re
from html import unescape
from urllib.parse import unquote, urlparse

from app.clients.http.urls import abs_url, host_key
from app.parsers.schema import LinksSpec

A_TAG_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
ATTR_RE = re.compile(
    r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.I,
)


def slug_name(url: str) -> str:
    path = urlparse(url.rstrip("/")).path
    parts = [p for p in path.split("/") if p]
    slug = unquote(parts[-1] if parts else "")
    return slug.replace("-", " ").replace("_", " ").strip() or slug


def plain_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return unescape(re.sub(r"\s+", " ", text)).strip()


def attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw or ""):
        out[match.group(1).lower()] = match.group(2) or match.group(3) or match.group(4) or ""
    return out


def extract_links(html: str, spec: LinksSpec, base_url: str) -> list[dict[str, str | None]]:
    href_re = re.compile(spec.href, re.I) if spec.href else None
    path_re = re.compile(spec.path, re.I) if spec.path else None
    skip_re = re.compile(spec.skip_href, re.I) if spec.skip_href else None
    class_need = spec.css_class
    name_mode = spec.name or "text"
    base_host = host_key(base_url)
    found: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for attr_s, body in A_TAG_RE.findall(html):
        info = attrs(attr_s)
        href = (info.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if class_need and class_need not in (info.get("class") or ""):
            continue
        if skip_re and skip_re.search(href):
            continue
        url = abs_url(base_url, href)
        if skip_re and skip_re.search(url):
            continue
        if host_key(url) != base_host:
            continue
        path = urlparse(url).path
        if href_re and not (href_re.search(href) or href_re.search(url) or href_re.search(path)):
            continue
        if path_re and not path_re.search(path):
            continue
        url = url.rstrip("/")
        if url in seen:
            continue
        seen.add(url)
        if name_mode == "title":
            name = unescape(info.get("title") or "").strip() or plain_text(body) or slug_name(url)
        elif name_mode == "slug":
            name = slug_name(url)
        else:
            name = plain_text(body) or unescape(info.get("title") or "").strip() or slug_name(url)
        found.append({"url": url, "name": name})
    return found
