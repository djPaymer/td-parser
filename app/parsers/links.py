from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from html import unescape
from urllib.parse import unquote, urlparse

from app.clients.http.urls import abs_url, host_key

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


SHARED_HOST_RE = re.compile(
    r"(?:^|\.)(?:facebook|instagram|linkedin|youtube|youtu\.be|twitter|x\.com|vk\.com|weibo|tiktok|douyin|"
    r"pinterest|wechat|qq\.com|baidu|google|gstatic|apple|microsoft|alibaba|aliexpress|1688|made-in-china|"
    r"globalsources|taobao|tmall|jd\.com|amazon|ebay|wikipedia|beian\.gov\.cn|miit\.gov\.cn|cnzz|"
    r"xing|whatsapp|telegram|t\.me|skype|zhihu|bilibili|indiamart|tradeindia)(?:\.|$)",
    re.I,
)


def foreign_hosts(html: str, base_url: str) -> Counter[str]:
    """Other hosts linked from the page, excluding social networks and marketplaces.

    A landing page whose links mostly point at one other host (a brand site that
    lives on the parent company's domain) is detected with this.
    """

    base_host = host_key(base_url)
    counts: Counter[str] = Counter()
    for attr_s, _ in A_TAG_RE.findall(html or ""):
        href = unescape((attrs(attr_s).get("href") or "").strip())
        if not href.lower().startswith(("http://", "https://", "//")):
            continue
        host = host_key(abs_url(base_url, href))
        if host and host != base_host and not SHARED_HOST_RE.search(host):
            counts[host] += 1
    return counts


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


def matches(regex: re.Pattern[str], anchor: Anchor) -> bool:
    """The product regex is matched against the raw href, the absolute URL and the path."""

    return bool(regex.search(anchor.href) or regex.search(anchor.url) or regex.search(anchor.path))


def _pick_name(anchor: Anchor, mode: str) -> str:
    if mode == "title":
        return anchor.title or anchor.text or slug_name(anchor.url)
    return anchor.text or anchor.title or slug_name(anchor.url)


def _name_quality(anchor: Anchor, mode: str) -> int:
    if mode == "title":
        return 2 if anchor.title else 1 if anchor.text else 0
    return 2 if anchor.text else 1 if anchor.title else 0


def extract_links(
    html: str, product_re: re.Pattern[str], base_url: str, name_mode: str = "text"
) -> list[dict[str, str]]:
    """Product links on the page. One row per URL; the best available name wins."""

    found: dict[str, dict[str, str]] = {}
    quality: dict[str, int] = {}
    for anchor in iter_anchors(html, base_url):
        if not matches(product_re, anchor):
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
