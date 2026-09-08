from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.dependencies import get_pipeline
from app.parsers.schema import SiteInstruction
from app.services.pipeline import CatalogPipeline, PipelineError
from app.store.instructions import StoredInstruction

router = APIRouter(tags=["pipeline"])


class UrlBody(BaseModel):
    url: str = Field(description="Manufacturer site root, e.g. https://www.example.com")
    refresh: bool = Field(default=False, description="Ignore the stored instruction and rebuild it")


class ParseBody(BaseModel):
    url: str
    instruction: SiteInstruction | None = Field(
        default=None, description="Optional; when omitted the stored instruction for the host is used"
    )


class SaveBody(BaseModel):
    url: str | None = Field(default=None, description="Site root; defaults to https://<host>")
    instruction: SiteInstruction


@router.post("/instruction", response_model=SiteInstruction, response_model_exclude_none=True)
async def build_instruction(
    body: UrlBody,
    pipeline: CatalogPipeline = Depends(get_pipeline),
) -> SiteInstruction:
    """Build (or return the stored) scrape instruction for a manufacturer site."""

    try:
        record = await pipeline.instruction(body.url, refresh=body.refresh)
    except PipelineError as exc:
        raise _http(exc) from exc
    return record.instruction


@router.get("/instructions")
async def list_instructions(pipeline: CatalogPipeline = Depends(get_pipeline)) -> list[dict]:
    try:
        records = await pipeline.list_instructions()
    except PipelineError as exc:
        raise _http(exc) from exc
    return [record.summary() for record in records]


@router.get("/instructions/{host}", response_model=StoredInstruction, response_model_exclude_none=True)
async def get_instruction(host: str, pipeline: CatalogPipeline = Depends(get_pipeline)) -> StoredInstruction:
    """Stored instruction with build diagnostics (`meta`)."""

    try:
        return await pipeline.get_instruction(host)
    except PipelineError as exc:
        raise _http(exc) from exc


@router.put("/instructions/{host}", response_model=StoredInstruction, response_model_exclude_none=True)
async def save_instruction(
    host: str,
    body: SaveBody,
    pipeline: CatalogPipeline = Depends(get_pipeline),
) -> StoredInstruction:
    """Save a hand-written / corrected instruction. Manual records are never rebuilt automatically."""

    try:
        return await pipeline.save_instruction(host, body.url, body.instruction)
    except PipelineError as exc:
        raise _http(exc) from exc


@router.delete("/instructions/{host}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instruction(host: str, pipeline: CatalogPipeline = Depends(get_pipeline)) -> None:
    try:
        await pipeline.delete_instruction(host)
    except PipelineError as exc:
        raise _http(exc) from exc


@router.post("/parse")
async def parse_catalog(
    body: ParseBody,
    pipeline: CatalogPipeline = Depends(get_pipeline),
) -> dict:
    """Collect product URLs using the given (or stored) instruction."""

    try:
        return await pipeline.parse(body.url, body.instruction)
    except PipelineError as exc:
        raise _http(exc) from exc


def _http(exc: PipelineError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))
