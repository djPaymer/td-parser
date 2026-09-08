"""Persistent storage of scrape instructions, one record per site host."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import InstructionRow
from app.parsers.schema import SiteInstruction

SOURCE_MANUAL = "manual"


class StoredInstruction(BaseModel):
    host: str
    site_url: str
    instruction: SiteInstruction
    source: str = Field(description="manual | agent-llm | agent-heuristic")
    meta: dict[str, Any] = Field(default_factory=dict, description="Diagnostics from the last build")
    created_at: datetime
    updated_at: datetime

    @property
    def is_manual(self) -> bool:
        return self.source == SOURCE_MANUAL

    def summary(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "site_url": self.site_url,
            "source": self.source,
            "listing": self.instruction.url,
            "href": self.instruction.links.href,
            "updated_at": self.updated_at,
        }


class InstructionStore(Protocol):
    async def get(self, host: str) -> StoredInstruction | None: ...

    async def put(self, record: StoredInstruction) -> StoredInstruction: ...

    async def delete(self, host: str) -> bool: ...

    async def list(self, limit: int = 500) -> list[StoredInstruction]: ...

    async def close(self) -> None: ...


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


class DbInstructionStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, host: str) -> StoredInstruction | None:
        async with self._sessions() as session:
            row = await session.get(InstructionRow, host)
            return _to_dto(row) if row else None

    async def put(self, record: StoredInstruction) -> StoredInstruction:
        payload = {
            "host": record.host,
            "site_url": record.site_url,
            "instruction": record.instruction.model_dump(by_alias=True, exclude_none=True, mode="json"),
            "source": record.source,
            "meta": record.meta,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        stmt = insert(InstructionRow).values(**payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[InstructionRow.host],
            set_={
                "site_url": stmt.excluded.site_url,
                "instruction": stmt.excluded.instruction,
                "source": stmt.excluded.source,
                "meta": stmt.excluded.meta,
                "updated_at": stmt.excluded.updated_at,
            },
        ).returning(InstructionRow)
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _to_dto(row)

    async def delete(self, host: str) -> bool:
        async with self._sessions() as session:
            row = await session.get(InstructionRow, host)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def list(self, limit: int = 500) -> list[StoredInstruction]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(InstructionRow).order_by(InstructionRow.updated_at.desc()).limit(limit)
                )
            ).all()
            return [_to_dto(row) for row in rows]

    async def close(self) -> None:
        return None


def _to_dto(row: InstructionRow) -> StoredInstruction:
    return StoredInstruction(
        host=row.host,
        site_url=row.site_url,
        instruction=SiteInstruction.model_validate(row.instruction),
        source=row.source,
        meta=row.meta or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
