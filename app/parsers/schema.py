from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LinksSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    href: str | None = None
    path: str | None = None
    css_class: str | None = Field(default=None, alias="class")
    name: str = "text"
    skip_href: str | None = None


class PaginationSpec(BaseModel):
    param: str = "page"
    declared_count: str | None = None


class SiteInstruction(BaseModel):
    """How to find product URLs. Site origin comes from the parse request URL."""

    model_config = ConfigDict(extra="ignore")

    engine: str = "html"
    url: str = "/"
    links: LinksSpec = Field(default_factory=LinksSpec)
    pagination: PaginationSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def flatten_legacy_categories(cls, data):
        if not isinstance(data, dict):
            return data
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
