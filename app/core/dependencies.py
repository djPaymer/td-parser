from fastapi import Request

from app.clients.http import HtmlFetcher
from app.core.config import settings
from app.services.pipeline import CatalogPipeline


def get_html_fetcher(request: Request) -> HtmlFetcher:
    return request.app.state.fetcher


def get_pipeline(request: Request) -> CatalogPipeline:
    return CatalogPipeline(
        request.app.state.fetcher,
        request.app.state.agent,
        request.app.state.store,
        ttl_days=settings.store.ttl_days,
    )
