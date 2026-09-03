from app.agents.instruction import AgentError, InstructionAgent
from app.clients.http.fetch import HtmlFetchError, HtmlFetcher
from app.parsers.html import ParseError, parse_site
from app.parsers.schema import SiteInstruction


class PipelineError(RuntimeError):
    pass


class CatalogPipeline:
    def __init__(self, fetcher: HtmlFetcher, agent: InstructionAgent | None) -> None:
        self._fetcher = fetcher
        self._agent = agent

    async def instruction(self, url: str) -> SiteInstruction:
        if self._agent is None:
            raise PipelineError("AI agent is not configured")
        try:
            return await self._agent.build_instruction(url, self._fetcher)
        except (AgentError, HtmlFetchError) as exc:
            raise PipelineError(str(exc)) from exc

    async def parse(self, url: str, instruction: SiteInstruction) -> dict:
        try:
            return await parse_site(url, instruction, self._fetcher)
        except (ParseError, HtmlFetchError, ValueError) as exc:
            raise PipelineError(str(exc)) from exc
