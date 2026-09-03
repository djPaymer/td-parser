from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import get_pipeline
from app.parsers.schema import SiteInstruction
from app.services.pipeline import CatalogPipeline, PipelineError

router = APIRouter(tags=["pipeline"])


class UrlBody(BaseModel):
    url: str


class ParseBody(BaseModel):
    url: str
    instruction: SiteInstruction


@router.post("/instruction")
async def build_instruction(
    body: UrlBody,
    pipeline: CatalogPipeline = Depends(get_pipeline),
) -> dict:
    try:
        instruction = await pipeline.instruction(body.url)
    except PipelineError as exc:
        raise _http(exc) from exc
    return instruction.model_dump(by_alias=True, exclude_none=True)


@router.post("/parse")
async def parse_catalog(
    body: ParseBody,
    pipeline: CatalogPipeline = Depends(get_pipeline),
) -> dict:
    try:
        return await pipeline.parse(body.url, body.instruction)
    except PipelineError as exc:
        raise _http(exc) from exc


def _http(exc: PipelineError) -> HTTPException:
    text = str(exc)
    if "not configured" in text:
        return HTTPException(status_code=503, detail=text)
    return HTTPException(status_code=502, detail=text)
