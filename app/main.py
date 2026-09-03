from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from app.agents.instruction import AgentError, InstructionAgent
from app.api import router as api_router
from app.clients.http import HtmlFetcher
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    fetcher = HtmlFetcher(
        timeout=settings.fetch.timeout,
        user_agent=settings.fetch.user_agent,
        max_bytes=settings.fetch.max_bytes,
        delay_seconds=settings.fetch.delay_seconds,
        verify_ssl=settings.fetch.verify_ssl,
    )
    agent = None
    if settings.agent.api_key:
        try:
            agent = InstructionAgent(settings.agent)
        except AgentError:
            agent = None
    app.state.fetcher = fetcher
    app.state.agent = agent
    try:
        yield
    finally:
        await fetcher.aclose()


main_app = FastAPI(
    title="TD Parser",
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

main_app.include_router(
    api_router,
    prefix=settings.api.prefix,
)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:main_app",
        host=settings.run.host,
        port=settings.run.port,
        reload=True,
    )
