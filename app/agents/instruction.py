from __future__ import annotations

import json
import re
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI

from app.agents.outline import (
    catalog_candidate_urls,
    count_hits,
    format_outline,
    page_links,
    retry_hint,
    sample_paths,
)
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher, Page
from app.clients.http.urls import origin
from app.core.config import AgentConfig
from app.parsers.schema import SiteInstruction

INSTRUCTIONS = (
    "You write a scrape instruction to find PRODUCT detail pages only. "
    "Do NOT list products. Infer a compact regex from the listed path shapes. "
    "Reply with one small JSON object only, no markdown, under 500 characters: "
    '{"engine":"html","url":"/catalog-index",'
    '"links":{"href":"/product/\\\\d+","name":"text"},'
    '"pagination":{"param":"page"}}. '
    "url is a catalog index (for example /products or /mall/), not a product page. "
    "links.href is a SHORT regex for a path shape, usually under 80 characters. "
    "Example: /product/\\d+ or /mall/dt_[a-z0-9_]+\\.html. "
    "Never enumerate URLs, never use a long a|b|c alternation. "
    "Do not invent goods-\\d+.html, /product/\\d+, or prolist_t unless that shape is listed. "
    "Prefer name text. Omit class unless needed. Omit pagination if there is no pager."
)

EXTRA_CATALOG_PAGES = 2
MAX_HREF_LEN = 180
JSON_RETRY_HINT = (
    "\n\nYour previous reply was cut off or invalid. "
    "Return one complete JSON object under 500 characters. "
    "links.href must be a short regex like /product/\\d+, not a list of URLs."
)


class AgentError(RuntimeError):
    pass


class InstructionAgent:
    def __init__(self, config: AgentConfig) -> None:
        if not config.api_key:
            raise AgentError("APP_CONFIG__AGENT__API_KEY is empty")
        base = (config.base_url or "https://ai.api.cloud.yandex.net/v1").rstrip("/")
        kwargs: dict[str, Any] = {"api_key": config.api_key, "base_url": base}
        if config.folder_id:
            kwargs["project"] = config.folder_id
        self._llm = AsyncOpenAI(**kwargs)
        self._model = _yandex_model_uri(config.model, config.folder_id)
        self._html_limit = config.html_limit
        self._max_output_tokens = config.max_output_tokens
        self._max_pages = max(1, config.max_pages)

    async def build_instruction(self, site_url: str, fetcher: HtmlFetcher) -> SiteInstruction:
        pages = await self._collect_pages(site_url, fetcher)
        outline = format_outline(site_url, pages, self._html_limit)
        instruction = await self._instruction_from(outline)
        base = origin(site_url)
        hits, href_hits = count_hits(instruction, pages, base)
        if hits > 0 and _compact_href(instruction):
            return instruction
        instruction = await self._instruction_from(outline + "\n\n" + retry_hint(instruction, pages, hits, href_hits))
        hits, _ = count_hits(instruction, pages, base)
        if hits > 0 and _compact_href(instruction):
            return instruction
        samples = ", ".join(sample_paths(pages, 12)) or "(none)"
        raise AgentError(
            f"instruction href {instruction.links.href!r} matched 0 product links. Sample paths: {samples}"
        )

    async def _collect_pages(self, site_url: str, fetcher: HtmlFetcher) -> list[Page]:
        home = await fetcher.get(site_url, origin=site_url)
        pages = [home]
        extra = min(EXTRA_CATALOG_PAGES, max(0, self._max_pages - 1))
        if extra <= 0:
            return pages
        seen = {home.url.rstrip("/")}
        links = page_links(home.html, home.url or site_url)
        for url in catalog_candidate_urls(links, site_url, extra):
            key = url.rstrip("/")
            if key in seen:
                continue
            try:
                page = await fetcher.get(url, origin=site_url)
            except HtmlFetchError:
                continue
            seen.add(page.url.rstrip("/"))
            pages.append(page)
            if len(pages) >= extra + 1:
                break
        return pages

    async def _instruction_from(self, user_input: str) -> SiteInstruction:
        payload = await self._complete(user_input)
        try:
            return SiteInstruction.model_validate(payload)
        except Exception as exc:
            raise AgentError(f"invalid instruction JSON: {exc}") from exc

    async def _complete(self, user_input: str) -> dict:
        text = await self._generate(user_input)
        payload = _parse_json_object(text)
        if payload:
            return payload
        text = await self._generate(user_input + JSON_RETRY_HINT)
        payload = _parse_json_object(text)
        if payload:
            return payload
        snippet = re.sub(r"\s+", " ", text or "")[:240]
        raise AgentError(
            "LLM did not return an instruction JSON object"
            + (f": {snippet}" if snippet else "")
        )

    async def _generate(self, user_input: str) -> str:
        try:
            response = await self._llm.responses.create(
                model=self._model,
                temperature=0.2,
                instructions=INSTRUCTIONS,
                input=user_input,
                max_output_tokens=self._max_output_tokens,
            )
        except APIConnectionError as exc:
            raise AgentError(f"LLM {self._model}: Connection error.") from exc
        except APIError as exc:
            raise AgentError(f"LLM {self._model}: {exc}") from exc
        return _response_text(response)


def _compact_href(instruction: SiteInstruction) -> bool:
    href = instruction.links.href or ""
    return bool(href) and len(href) <= MAX_HREF_LEN and href.count("|") <= 5


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None) or ""
    if str(text).strip():
        return str(text)
    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")
        for part in content or []:
            value = getattr(part, "text", None)
            if value is None and isinstance(part, dict):
                value = part.get("text")
            if isinstance(value, str) and value.strip():
                chunks.append(value)
    return "\n".join(chunks)


def _yandex_model_uri(model: str, folder_id: str) -> str:
    name = (model or "").strip()
    folder = (folder_id or "").strip()
    if not name:
        raise AgentError("APP_CONFIG__AGENT__MODEL is empty")
    if name.startswith(("gpt://", "ds://", "http://", "https://")):
        return name
    if folder:
        return f"gpt://{folder}/{name.lstrip('/')}"
    return name


def _parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
