import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from app.agents.instruction import InstructionAgent
from app.agents.llm import LlmClient, LlmError
from app.api import router as api_router
from app.clients.http import HtmlFetcher
from app.core.config import settings
from app.db.session import create_engine, create_session_factory, masked_url
from app.store import DbInstructionStore

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fetcher = HtmlFetcher(
        timeout=settings.fetch.timeout,
        user_agent=settings.fetch.user_agent,
        max_bytes=settings.fetch.max_bytes,
        delay_seconds=settings.fetch.delay_seconds,
        verify_ssl=settings.fetch.verify_ssl,
    )
    llm = None
    if settings.agent.api_key:
        try:
            llm = LlmClient(settings.agent)
        except LlmError as exc:
            log.warning("LLM disabled: %s", exc)
    else:
        log.warning("APP_CONFIG__AGENT__API_KEY is empty: instructions will be built by heuristics only")
    engine = None
    store = None
    if settings.db.url:
        engine = create_engine(settings.db.url)
        store = DbInstructionStore(create_session_factory(engine))
        log.info("instruction store: %s", masked_url(settings.db.url))
    else:
        log.warning("APP_CONFIG__DB__URL is empty: instruction storage is disabled")
    app.state.fetcher = fetcher
    app.state.agent = InstructionAgent(settings.agent, llm)
    app.state.store = store
    try:
        yield
    finally:
        await fetcher.aclose()
        if store is not None:
            await store.close()
        if engine is not None:
            await engine.dispose()


main_app = FastAPI(
    title="TD Parser",
    description="Finds the product catalog of a manufacturer site, builds a scrape instruction and collects product URLs.",
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
