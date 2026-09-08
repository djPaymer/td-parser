from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _check_regex(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        re.compile(value, re.I)
    except re.error as exc:
        raise ValueError(f"invalid regex {value!r}: {exc}") from exc
    return value


class LinksSpec(BaseModel):
    """How to recognise a product link on a listing page."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    href: str | None = Field(default=None, description="Regex matched against href / URL / path")
    path: str | None = Field(default=None, description="Extra regex matched against the URL path only")
    css_class: str | None = Field(default=None, alias="class", description="Substring required in <a class>")
    name: str = Field(default="text", description="Where to take the product name: text | title | slug")
    skip_href: str | None = Field(default=None, description="Regex; matching links are ignored")

    _href_ok = field_validator("href", "path", "skip_href")(_check_regex)

    @field_validator("name")
    @classmethod
    def _name_mode(cls, value: str) -> str:
        value = (value or "text").strip().lower()
        return value if value in {"text", "title", "slug"} else "text"


class CategoriesSpec(BaseModel):
    """Listing / category pages the parser is allowed to follow while looking for products."""

    model_config = ConfigDict(extra="ignore")

    href: str = Field(description="Regex for category page paths (matched like links.href)")
    max_pages: int = Field(default=300, ge=1, le=2000, description="Upper bound on category pages visited")

    _href_ok = field_validator("href")(_check_regex)


class PaginationSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    param: str = "page"
    declared_count: str | None = None


class SiteInstruction(BaseModel):
    """How to find product URLs. Site origin comes from the parse request URL."""

    model_config = ConfigDict(extra="ignore")

    engine: str = "html"
    url: str = Field(default="/", description="Catalog entry path, e.g. /products")
    links: LinksSpec = Field(default_factory=LinksSpec)
    categories: CategoriesSpec | None = None
    pagination: PaginationSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def flatten_legacy_categories(cls, data):
        if not isinstance(data, dict):
            return data
        cats = data.get("categories")
        if isinstance(cats, dict) and not cats.get("href"):
            data = {k: v for k, v in data.items() if k != "categories"}
        if "products" in data and "links" not in data:
            products = data.get("products") or {}
            cats = data.get("categories") or {}
            listing = (
                (products.get("url") if isinstance(products, dict) else None)
                or (cats.get("url") if isinstance(cats, dict) else None)
                or "/"
            )
            links = products.get("links") if isinstance(products, dict) else None
            pagination = products.get("pagination") if isinstance(products, dict) else None
            return {
                "engine": data.get("engine") or "html",
                "url": listing,
                "links": links or {},
                "pagination": pagination,
            }
        return data
