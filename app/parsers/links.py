from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote, urlparse

from app.clients.http.urls import abs_url, host_key
from app.parsers.schema import LinksSpec

A_TAG_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
ATTR_RE = re.compile(
    r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.I,
)
NOISE_BLOCK_RE = re.compile(
    r"<(script|style|svg|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.I | re.S,
)
IMG_RE = re.compile(r"<(?:img|picture|svg)\b", re.I)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

MAX_PATH_DEPTH = 6
FILE_RE = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|webp|svg|bmp|ico|zip|rar|7z|gz|docx?|xlsx?|pptx?|mp[34]|avi|mov|"
    r"xml|json|css|js|woff2?|ttf|eot|txt|csv|dwg|stp|step|exe|msi|dmg|apk)$",
    re.I,
)
# path segments that never lead to a product catalog
SKIP_SEGMENT_RE = re.compile(
    r"^(?:news|blog|blogs|about|about-us|aboutus|company|contact|contacts|contact-us|contactus|"
    r"inquiry|enquiry|career|careers|jobs|login|logout|register|signin|signup|cart|basket|checkout|"
    r"account|my-account|profile|privacy|policy|terms|legal|cookies?|sitemap|search|feedback|faq|"
    r"press|tag|tags|cdn-cgi|wp-content|wp-json|wp-admin|wp-login\.php|feed|rss|lang|language|"
    r"share|print|compare|wishlist|favou?rites?|auth|admin|user|users|bitrix|ajax|api|static|"
    r"assets|images?|img|css|js|fonts?|upload|uploads|media|video|videos|gallery|events?|"
    r"exhibitions?|certificates?|honou?rs?|partners?|dealers?|distributors?|where-to-buy|"
    r"warranty|service|services|support|help|downloads?|manuals?|articles?|"
    # common Russian site sections
    r"stati|statya|novosti|aktsii|akcii|promo|promo_products|o-kompanii|kontakty|dostavka|oplata|"
    r"garantiya|servis|service-centers?|servisnye-tsentry|otzyvy|reviews?|gde-kupit|partnery|"
    r"vakansii|karta-sayta|poisk|lichnyy-kabinet|korzina|sravnenie|izbrannoe)$",
    re.I,
)


def is_content_path(path: str) -> bool:
    """False for the home page, files and service sections (news, cart, login...)."""

    if path == "/" or FILE_RE.search(path):
        return False
    segs = [s for s in path.split("/") if s]
    if len(segs) > MAX_PATH_DEPTH:
        return False
    return not any(SKIP_SEGMENT_RE.match(unquote(s)) for s in segs)


@dataclass(slots=True)
class Anchor:
    """One <a> element resolved against the page it was found on."""

    url: str
    path: str
    href: str
    text: str
    title: str
    css_class: str
    has_img: bool

    @property
    def name(self) -> str:
        return self.text or self.title or slug_name(self.url)


def slug_name(url: str) -> str:
    path = urlparse(url.rstrip("/")).path
    parts = [p for p in path.split("/") if p]
    slug = unquote(parts[-1] if parts else "")
    slug = re.sub(r"\.(?:html?|php|aspx?|jsp|shtml)$", "", slug, flags=re.I)
    return slug.replace("-", " ").replace("_", " ").strip() or slug


def plain_text(html: str) -> str:
    text = NOISE_BLOCK_RE.sub(" ", html or "")
    text = TAG_RE.sub(" ", text)
    return unescape(SPACE_RE.sub(" ", text)).strip()


def attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw or ""):
        out[match.group(1).lower()] = match.group(2) or match.group(3) or match.group(4) or ""
    return out


def compile_regex(pattern: str | None, field: str = "regex") -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"invalid {field} regex {pattern!r}: {exc}") from exc


def iter_anchors(html: str, base_url: str) -> list[Anchor]:
    """All same-host anchors on the page, in document order, one entry per <a>."""

    base_host = host_key(base_url)
    out: list[Anchor] = []
    for attr_s, body in A_TAG_RE.findall(html or ""):
        info = attrs(attr_s)
        href = unescape((info.get("href") or "").strip())
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        url = abs_url(base_url, href)
        if host_key(url) != base_host:
            continue
        parsed = urlparse(url)
        out.append(
            Anchor(
                url=url.rstrip("/"),
                path=parsed.path.rstrip("/") or "/",
                href=href,
                text=plain_text(body),
                title=unescape(info.get("title") or "").strip(),
                css_class=info.get("class") or "",
                has_img=bool(IMG_RE.search(body)),
            )
        )
    return out


def _matches(spec_re: re.Pattern[str] | None, anchor: Anchor) -> bool:
    if spec_re is None:
        return True
    return bool(
        spec_re.search(anchor.href) or spec_re.search(anchor.url) or spec_re.search(anchor.path)
    )


def _pick_name(anchor: Anchor, mode: str) -> str:
    if mode == "title":
        return anchor.title or anchor.text or slug_name(anchor.url)
    if mode == "slug":
        return slug_name(anchor.url)
    return anchor.text or anchor.title or slug_name(anchor.url)


def _name_quality(anchor: Anchor, mode: str) -> int:
    if mode == "slug":
        return 0
    if mode == "title":
        return 2 if anchor.title else 1 if anchor.text else 0
    return 2 if anchor.text else 1 if anchor.title else 0


def extract_links(html: str, spec: LinksSpec, base_url: str) -> list[dict[str, str | None]]:
    """Links matching the spec. One row per URL; the best available name wins."""

    href_re = compile_regex(spec.href, "links.href")
    path_re = compile_regex(spec.path, "links.path")
    skip_re = compile_regex(spec.skip_href, "links.skip_href")
    class_need = spec.css_class
    name_mode = spec.name or "text"
    found: dict[str, dict[str, str | None]] = {}
    quality: dict[str, int] = {}
    for anchor in iter_anchors(html, base_url):
        if class_need and class_need not in anchor.css_class:
            continue
        if skip_re and (skip_re.search(anchor.href) or skip_re.search(anchor.url)):
            continue
        if not _matches(href_re, anchor):
            continue
        if path_re and not path_re.search(anchor.path):
            continue
        q = _name_quality(anchor, name_mode)
        if anchor.url in found:
            if q > quality[anchor.url]:
                found[anchor.url]["name"] = _pick_name(anchor, name_mode)
                quality[anchor.url] = q
            continue
        found[anchor.url] = {"url": anchor.url, "name": _pick_name(anchor, name_mode)}
        quality[anchor.url] = q
    return list(found.values())
