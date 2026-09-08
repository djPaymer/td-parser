from __future__ import annotations

import logging
from datetime import timedelta

from app.agents.instruction import AgentError, InstructionAgent
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher
from app.clients.http.urls import host_key, origin
from app.parsers.html import ParseError, parse_site
from app.parsers.schema import SiteInstruction
from app.store.instructions import SOURCE_MANUAL, InstructionStore, StoredInstruction, now_utc

log = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    status_code = 502


class NotConfiguredError(PipelineError):
    status_code = 503


class NotFoundError(PipelineError):
    status_code = 404


class BadRequestError(PipelineError):
    status_code = 400


class CatalogPipeline:
    def __init__(
        self,
        fetcher: HtmlFetcher,
        agent: InstructionAgent | None,
        store: InstructionStore | None = None,
        ttl_days: int = 0,
    ) -> None:
        self._fetcher = fetcher
        self._agent = agent
        self._store = store
        self._ttl = timedelta(days=ttl_days) if ttl_days > 0 else None

    # ------------------------------------------------------------ instructions

    async def instruction(self, url: str, *, refresh: bool = False) -> StoredInstruction:
        """Stored instruction for the site if it is fresh, otherwise build (and store) a new one."""

        host = _host(url)
        if not refresh and self._store is not None:
            record = await self._store.get(host)
            if record is not None and self._is_fresh(record):
                return record
        if self._agent is None:
            raise NotConfiguredError("AI agent is not configured")
        try:
            result = await self._agent.build_instruction(url, self._fetcher)
        except (AgentError, HtmlFetchError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc
        stamp = now_utc()
        record = StoredInstruction(
            host=host,
            site_url=origin(url),
            instruction=result.instruction,
            source=f"agent-{result.meta.get('source', 'llm')}",
            meta=result.meta,
            created_at=stamp,
            updated_at=stamp,
        )
        if self._store is not None:
            record = await self._store.put(record)
        return record

    async def save_instruction(self, host: str, url: str | None, instruction: SiteInstruction) -> StoredInstruction:
        store = self._require_store()
        host = _host(host)
        stamp = now_utc()
        record = StoredInstruction(
            host=host,
            site_url=origin(url) if url else f"https://{host}",
            instruction=instruction,
            source=SOURCE_MANUAL,
            meta={},
            created_at=stamp,
            updated_at=stamp,
        )
        return await store.put(record)

    async def get_instruction(self, host: str) -> StoredInstruction:
        record = await self._require_store().get(_host(host))
        if record is None:
            raise NotFoundError(f"no stored instruction for {host}")
        return record

    async def list_instructions(self) -> list[StoredInstruction]:
        return await self._require_store().list()

    async def delete_instruction(self, host: str) -> None:
        if not await self._require_store().delete(_host(host)):
            raise NotFoundError(f"no stored instruction for {host}")

    # ------------------------------------------------------------------ parse

    async def parse(self, url: str, instruction: SiteInstruction | None) -> dict:
        if instruction is None:
            if self._store is None:
                raise BadRequestError("instruction is required (storage is disabled)")
            record = await self._store.get(_host(url))
            if record is None:
                raise NotFoundError(f"no stored instruction for {_host(url)}; call POST /instruction first")
            instruction = record.instruction
        try:
            return await parse_site(url, instruction, self._fetcher)
        except (ParseError, HtmlFetchError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc

    # ---------------------------------------------------------------- helpers

    def _is_fresh(self, record: StoredInstruction) -> bool:
        if record.is_manual or self._ttl is None:
            return True
        return now_utc() - record.updated_at < self._ttl

    def _require_store(self) -> InstructionStore:
        if self._store is None:
            raise NotConfiguredError("instruction storage is disabled (APP_CONFIG__DB__URL is empty)")
        return self._store


def _host(url_or_host: str) -> str:
    host = host_key(url_or_host if "://" in url_or_host else f"https://{url_or_host}")
    if not host:
        raise BadRequestError(f"bad URL: {url_or_host}")
    return host
