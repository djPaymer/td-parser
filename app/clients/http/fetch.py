from __future__ import annotations

import asyncio
import re
from typing import Protocol

import httpx
from pydantic import BaseModel

from app.clients.http.urls import host_key, origin, same_host

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
BROWSER_LANG = "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"


class Page(BaseModel):
    url: str
    status: int
    html: str


class HtmlFetchError(RuntimeError):
    pass


class Fetcher(Protocol):
    """What the crawler, collector and runner need: ``HtmlFetcher`` (httpx) or ``BrowserFetcher`` (Playwright)."""

    async def get(self, url: str, *, origin: str | None = None) -> Page: ...

    async def aclose(self) -> None: ...


class HtmlFetcher:
    """Downloads HTML pages over plain HTTP; the fast path that works for server-rendered sites."""

    def __init__(
        self,
        timeout: float = 45,
        user_agent: str | None = None,
        max_bytes: int = 3_000_000,
        delay_seconds: float = 0.35,
        verify_ssl: bool = False,
    ) -> None:
        self._max_bytes = max_bytes
        self._delay = delay_seconds
        self._last_host: str | None = None
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            verify=verify_ssl,
            headers={
                "User-Agent": user_agent
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": BROWSER_ACCEPT,
                "Accept-Language": BROWSER_LANG,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, url: str, *, origin: str | None = None) -> Page:
        if origin and not same_host(origin, url):
            raise HtmlFetchError(f"refusing other host: {url}")
        host = host_key(url)
        if self._delay and self._last_host is not None:
            await asyncio.sleep(self._delay)
        self._last_host = host
        try:
            response = await self._http.get(url, headers={"Referer": _referer(url, origin)})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HtmlFetchError(
                f"HTTP {exc.response.status_code} GET {exc.request.url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise HtmlFetchError(f"Failed GET {url}: {exc}") from exc
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and not any(t in content_type for t in HTML_TYPES):
            raise HtmlFetchError(f"not HTML ({content_type.split(';')[0]}) GET {response.url}")
        raw = response.content[: self._max_bytes]
        html = decode_html(raw, response.charset_encoding)
        return Page(url=str(response.url), status=response.status_code, html=html)


HTML_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")
META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w-]+)""", re.I)


def decode_html(raw: bytes, header_charset: str | None) -> str:
    """Decode with the header charset, else <meta charset>, else UTF-8; never raises."""

    candidates: list[str] = []
    if header_charset:
        candidates.append(header_charset)
    match = META_CHARSET_RE.search(raw[:8192])
    if match:
        candidates.append(match.group(1).decode("ascii", errors="ignore"))
    candidates.append("utf-8")
    for name in candidates:
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    for name in candidates:
        try:
            return raw.decode(name, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


def _referer(url: str, site: str | None) -> str:
    base = site or url
    try:
        return origin(base) + "/"
    except ValueError:
        return url
