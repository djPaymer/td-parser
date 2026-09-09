"""Extract name, description and specifications from a product page.

Deterministic and site-agnostic.  Sources, in order of trust:

* ``<script type="application/ld+json">`` with ``@type: Product`` (name,
  description, sku, image, ``additionalProperty``);
* Open Graph / ``<meta name="description">``;
* the DOM: ``<h1>`` for the name, two-column tables / ``<dl>`` / ``key: value``
  list items for the specs, the largest text block whose class or id says
  "description" for the description.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.clients.http.urls import abs_url

NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "header", "footer", "nav", "form", "button")
NOISE_CLASS_RE = re.compile(
    r"breadcrumb|cookie|popup|modal|share|social|comment|sidebar|\bmenu\b|footer|\bnav\b|navbar|navigation|"
    r"related|recommend|similar|also-like|newsletter|subscribe|inquiry|enquiry|login|search",
    re.I,
)
SPEC_CONTAINER_RE = re.compile(
    r"spec|param|attr|feature|tech|charact|properties|detail|harakt|характ|параметр|参数|规格|技术",
    re.I,
)
DESC_CONTAINER_RE = re.compile(
    r"desc|description|detail|intro|overview|summary|about|content|text|feature|product-info|productinfo|"
    r"opisanie|описание|简介|详情|介绍",
    re.I,
)
KEY_VALUE_RE = re.compile(r"^\s*([^:：]{1,60}?)\s*[:：]\s*(.{1,400}?)\s*$", re.S)
BAD_KEY_RE = re.compile(
    r"tel|phone|mobile|fax|e-?mail|address|copyright|whatsapp|wechat|skype|qq|website|https?|hotline|"
    r"телефон|адрес|почта|电话|邮箱|地址|传真",
    re.I,
)
BAD_VALUES = {"nan", "null", "undefined", "none", "n/a", "-", "—"}
CODE_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9._-]{2,24}$")
SKU_KEY_RE = re.compile(r"^(?:model|model no\.?|item|item no\.?|art\.?|article|sku|code|part no\.?|型号|货号|артикул|код)$", re.I)
TITLE_SEP_RE = re.compile(r"\s+[|\-–—«»]\s+")
SPACE_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
NEWLINES_RE = re.compile(r"\n\s*\n+")

MAX_DESCRIPTION = 5000
MIN_SPECS_BEFORE_GLOBAL = 3
MIN_DESCRIPTION = 60
DESC_WEIGHTS = (1.0, 1.0, 0.6, 0.3)  # unit, parent, grandparent, great-grandparent
DESC_BONUS = 1.5
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
MAX_SPEC_VALUE = 400
MAX_NAME = 200


@dataclass(slots=True)
class ProductData:
    url: str
    name: str = ""
    description: str = ""
    specs: dict[str, str] = field(default_factory=dict)
    specs_table: str = ""  # multi-column tables (model matrices) rendered as text
    sku: str = ""
    image: str = ""

    @property
    def specs_text(self) -> str:
        lines = [f"{k}: {v}" for k, v in self.specs.items()]
        if self.specs_table:
            if lines:
                lines.append("")
            lines.append(self.specs_table)
        return "\n".join(lines)


def extract_product(html: str, url: str, fallback_name: str = "") -> ProductData:
    soup = BeautifulSoup(html or "", "lxml")
    data = ProductData(url=url)

    ld = _json_ld_product(soup)
    meta = _meta(soup)
    heading = _h1(soup)  # before noise removal: some themes wrap the product title in <header>

    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True, attrs={"class": NOISE_CLASS_RE}):
        tag.decompose()
    for tag in soup.find_all(True, id=NOISE_CLASS_RE):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")  # keeps "Key: value<br>Key: value" blocks splittable

    title_parts = _title_parts(meta.get("og:title", "")) + _title_parts(_title(soup))
    # h1 first: JSON-LD names are often the series name, the heading carries the model code
    data.name = _name(
        base_candidates=[heading, str(ld.get("name") or ""), *title_parts[:1], fallback_name],
        extenders=[heading, fallback_name],
    )

    specs, table_text = _specs(soup)
    for prop in ld.get("additionalProperty") or []:
        if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
            specs.setdefault(_clean(str(prop["name"])), _clean(str(prop["value"]))[:MAX_SPEC_VALUE])
    data.specs = specs
    data.specs_table = table_text

    data.description = _strip_spec_lines(_description(soup, ld, meta), specs)
    data.sku = _clean(str(ld.get("sku") or ld.get("mpn") or ""))
    if not data.sku:
        for key, value in specs.items():
            if SKU_KEY_RE.match(key.strip()):
                data.sku = value
                break
    if not data.sku:
        data.sku = _guess_sku(data.name, url)
    image = ld.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url") or ""
    data.image = abs_url(url, str(image or meta.get("og:image") or "")) if (image or meta.get("og:image")) else ""
    return data


# --------------------------------------------------------------------------- sources


def _json_ld_product(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = script.string or script.get_text() or ""
        try:
            payload = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _walk_ld(payload):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(isinstance(k, str) and k.lower() in {"product", "productmodel", "individualproduct"} for k in kinds):
                return node
    return {}


def _walk_ld(payload: Any):
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from _walk_ld(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_ld(item)


def _meta(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        content = tag.get("content")
        if key and content and key not in out:
            out[key] = _clean(str(content))
    return out


def _h1(soup: BeautifulSoup) -> str:
    for h1 in soup.find_all("h1"):
        text = _clean(h1.get_text(" ", strip=True))
        if 2 <= len(text) <= MAX_NAME:
            return text
    return ""


def _title(soup: BeautifulSoup) -> str:
    return _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""


def _title_parts(title: str) -> list[str]:
    """``Product name | Site`` -> ["Product name", "Site"] (product part usually comes first)."""

    return [p.strip() for p in TITLE_SEP_RE.split(_clean(title)) if p.strip()]


def _name(base_candidates: list[str], extenders: list[str]) -> str:
    """First usable base; an extender that *contains* it and adds a model code wins.

    ``Multipurpose End Mills`` (JSON-LD) -> ``A40FX Multipurpose End Mills`` (h1),
    but not the whole listing card text ``A40FX Multipurpose End Mills METRIC ASIA ...``.
    """

    base = next((_clean(c)[:MAX_NAME] for c in base_candidates if c and _clean(c)), "")
    if not base or _has_code(base):
        return base[:MAX_NAME]
    best = base
    for candidate in extenders:
        value = _clean(candidate)
        if len(best) < len(value) <= len(base) * 1.6 and base.lower() in value.lower() and _has_code(value):
            if len(value.split()) - len(base.split()) <= 3:
                best = value
    return best[:MAX_NAME]


def _has_code(text: str) -> bool:
    return any(CODE_RE.match(tok.strip(".:;-")) for tok in re.split(r"[\s,/()]+", text))


def _guess_sku(name: str, url: str) -> str:
    """A model code is a token with both letters and digits: the first one in the name, else the URL's last segment."""

    for token in re.split(r"[\s,/()]+", name):
        token = token.strip(".:;-")
        if CODE_RE.match(token):
            return token
    last = url.rstrip("/").rsplit("/", 1)[-1]
    last = re.sub(r"\.(?:html?|php|aspx?)$", "", last, flags=re.I)
    return last if CODE_RE.match(last) else ""


# --------------------------------------------------------------------------- specs


def _specs(soup: BeautifulSoup) -> tuple[dict[str, str], str]:
    specs: dict[str, str] = {}
    tables: list[str] = []

    for table in soup.find_all("table"):
        if table.find_parent("table") is not None:
            continue
        rows = _table_rows(table)
        if len(rows) < 2:
            continue
        widths = {len(r) for r in rows}
        if widths <= {1, 2} and sum(1 for r in rows if len(r) == 2) >= 2:
            for row in rows:
                if len(row) == 2:
                    _put(specs, row[0], row[1])
        elif max(widths) >= 3 and sum(len(r) >= 3 for r in rows) >= 2:
            tables.append("\n".join(" | ".join(cell for cell in row) for row in rows if any(row)))

    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        values = dl.find_all("dd")
        for dt, dd in zip(terms, values):
            _put(specs, dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))

    containers = soup.find_all(True, attrs={"class": SPEC_CONTAINER_RE}) + soup.find_all(True, id=SPEC_CONTAINER_RE)
    _harvest_pairs(containers, specs)
    if len(specs) < MIN_SPECS_BEFORE_GLOBAL:
        # no dedicated spec block: take short "Key: value" leaf items from the whole page
        _harvest_pairs([soup], specs, strict=True)
    return specs, "\n\n".join(tables)


def _harvest_pairs(containers: list[Tag], specs: dict[str, str], *, strict: bool = False) -> None:
    seen_text: set[str] = set()
    for container in containers:
        for node in container.find_all(["li", "p", "div", "span", "tr", "td"]):
            if node.find(["li", "p", "div", "table", "ul", "tr"]) is not None:
                continue  # not a leaf: its children are visited on their own
            for line in node.get_text("\n", strip=True).split("\n"):
                text = _clean(line)
                if not text or text in seen_text or len(text) > 300:
                    continue
                seen_text.add(text)
                match = KEY_VALUE_RE.match(text)
                if not match:
                    continue
                key, value = match.group(1), match.group(2)
                if strict and (len(key) > 40 or len(key.split()) > 5 or len(value) > 200):
                    continue
                _put(specs, key, value)


def _table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = [_clean(td.get_text(" ", strip=True)) for td in tr.find_all(["th", "td"], recursive=False)]
        if any(cells):
            rows.append(cells)
    return rows


def _put(specs: dict[str, str], key: str, value: str) -> None:
    key = _clean(key).rstrip(":：").strip()
    value = _clean(value)
    if not key or not value or len(key) > 60 or key == value:
        return
    if BAD_KEY_RE.search(key) or not re.search(r"[^\W\d_]", key):
        return  # keys need a letter: "0: 00" comes from video players and timers
    if value.lower().rstrip("%") in BAD_VALUES:
        return
    specs.setdefault(key, value[:MAX_SPEC_VALUE])


# --------------------------------------------------------------------------- description


def _description(soup: BeautifulSoup, ld: dict[str, Any], meta: dict[str, str]) -> str:
    ld_desc = _clean_block(str(ld.get("description") or ""))
    dom = _dom_description(soup)
    # structured data may carry the full text; meta descriptions are truncated teasers (last resort)
    best = ld_desc if len(ld_desc) > len(dom) else dom
    if not best:
        best = _clean_block(meta.get("og:description") or meta.get("description") or "")
    return best[:MAX_DESCRIPTION]


def _dom_description(soup: BeautifulSoup) -> str:
    """The block with the most prose.

    Every leaf text unit (paragraph, list item, bare div/span text) votes for
    itself and its nearest ancestors with decaying weight; blocks whose class or
    id says "description" get a bonus.  Ties go to the smaller block, so a list
    of feature bullets beats the column that wraps it together with buttons.
    """

    for table in soup.find_all("table"):
        table.decompose()  # already captured as specs
    for lst in soup.find_all(["ul", "ol"]):
        if len(lst.find_all("a")) >= 3 and _link_ratio(lst) > 0.7:
            lst.decompose()  # breadcrumbs, pagers, tag clouds, tab strips

    scores: dict[int, tuple[Tag, float]] = {}
    for unit in soup.find_all(["p", "li", "div", "span", "td", "dd"]):
        if unit.find(["p", "li", "div", "ul", "ol", "table", "section", "article"]) is not None:
            continue
        text = _clean(unit.get_text(" ", strip=True))
        minimum = 20 if CJK_RE.search(text) else 30
        if len(text) < minimum or _link_ratio(unit) > 0.5:
            continue
        if all(KEY_VALUE_RE.match(line) for line in unit.get_text("\n", strip=True).split("\n") if line.strip()):
            continue  # spec pairs, not prose
        weight = float(min(len(text), 600))
        for level, node in enumerate([unit, *unit.parents]):
            if level >= len(DESC_WEIGHTS) or node.name in ("body", "html", "[document]"):
                break
            bonus = DESC_BONUS if _labelled(node, DESC_CONTAINER_RE) else 1.0
            current = scores.get(id(node), (node, 0.0))[1]
            scores[id(node)] = (node, current + weight * DESC_WEIGHTS[level] * bonus)
    if not scores:
        return ""
    ranked = sorted(scores.values(), key=lambda ns: (-ns[1], len(ns[0].get_text())))
    for node, _ in ranked[:5]:
        if _link_ratio(node) > 0.3:
            continue
        text = _clean_block(_block_text(node))
        if len(text) >= MIN_DESCRIPTION:
            return text
    return ""


def _labelled(node: Tag, pattern: re.Pattern[str]) -> bool:
    classes = node.get("class") or []
    ident = node.get("id") or ""
    return any(pattern.search(c) for c in classes) or bool(ident and pattern.search(ident))


def _strip_spec_lines(description: str, specs: dict[str, str]) -> str:
    """Drop description lines that are verbatim spec pairs (they live in the specs column)."""

    if not description or not specs:
        return description
    pairs = {re.sub(r"\s+", "", f"{k}:{v}").lower() for k, v in specs.items()}
    pairs |= {re.sub(r"\s+", "", f"{k}：{v}").lower() for k, v in specs.items()}
    kept = [line for line in description.split("\n") if re.sub(r"\s+", "", line).lower() not in pairs]
    return "\n".join(kept).strip()


def _block_text(node: Tag) -> str:
    return node.get_text("\n", strip=True)


def _link_ratio(node: Tag) -> float:
    total = len(node.get_text(" ", strip=True)) or 1
    linked = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    return linked / total


# --------------------------------------------------------------------------- text


def _clean(text: str) -> str:
    return SPACE_RE.sub(" ", (text or "").replace("\n", " ")).strip()


def _clean_block(text: str) -> str:
    lines = [SPACE_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    return NEWLINES_RE.sub("\n", "\n".join(line for line in lines if line)).strip()
