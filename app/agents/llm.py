from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import APIConnectionError, APIError, AsyncOpenAI

from app.core.config import AgentConfig

log = logging.getLogger(__name__)

JSON_RETRY_HINT = (
    "\n\nYour previous reply was not a valid JSON object. "
    "Reply with exactly one complete JSON object and nothing else."
)


class LlmError(RuntimeError):
    pass


class LlmClient:
    """Thin wrapper over the OpenAI-compatible Responses API (Yandex AI Studio)."""

    def __init__(self, config: AgentConfig) -> None:
        if not config.api_key:
            raise LlmError("APP_CONFIG__AGENT__API_KEY is empty")
        base = (config.base_url or "https://ai.api.cloud.yandex.net/v1").rstrip("/")
        kwargs: dict[str, Any] = {"api_key": config.api_key, "base_url": base}
        if config.folder_id:
            kwargs["project"] = config.folder_id
        self._llm = AsyncOpenAI(**kwargs)
        self.model = yandex_model_uri(config.model, config.folder_id)
        self._max_output_tokens = config.max_output_tokens
        self._temperature = config.temperature

    async def json_object(self, instructions: str, user_input: str) -> dict:
        """One JSON object from the model; retries once with a bigger budget / a hint."""

        budget = self._max_output_tokens
        text, truncated = await self._generate(instructions, user_input, budget)
        payload = parse_json_object(text)
        if payload:
            return payload
        if truncated:
            # reasoning models spend hidden tokens before answering; give them room
            budget *= 3
            log.info("LLM %s: output truncated at %d tokens, retrying with %d", self.model, budget // 3, budget)
            text, truncated = await self._generate(instructions, user_input, budget)
        else:
            text, truncated = await self._generate(instructions, user_input + JSON_RETRY_HINT, budget)
        payload = parse_json_object(text)
        if payload:
            return payload
        snippet = re.sub(r"\s+", " ", text or "")[:240]
        detail = " (output truncated)" if truncated else ""
        raise LlmError(f"LLM did not return a JSON object{detail}" + (f": {snippet}" if snippet else ""))

    async def text(self, instructions: str, user_input: str) -> str:
        text, _ = await self._generate(instructions, user_input, self._max_output_tokens)
        return text

    async def _generate(self, instructions: str, user_input: str, max_output_tokens: int) -> tuple[str, bool]:
        try:
            response = await self._llm.responses.create(
                model=self.model,
                temperature=self._temperature,
                instructions=instructions,
                input=user_input,
                max_output_tokens=max_output_tokens,
            )
        except APIConnectionError as exc:
            raise LlmError(f"LLM {self.model}: connection error ({type(exc).__name__})") from exc
        except APIError as exc:
            raise LlmError(f"LLM {self.model}: {exc}") from exc
        details = getattr(response, "incomplete_details", None)
        truncated = getattr(response, "status", None) == "incomplete" and getattr(details, "reason", "") == "max_output_tokens"
        return response_text(response), bool(truncated)


def response_text(response: Any) -> str:
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


def yandex_model_uri(model: str, folder_id: str) -> str:
    name = (model or "").strip()
    folder = (folder_id or "").strip()
    if not name:
        raise LlmError("APP_CONFIG__AGENT__MODEL is empty")
    if name.startswith(("gpt://", "ds://", "http://", "https://")):
        return name
    if folder:
        return f"gpt://{folder}/{name.lstrip('/')}"
    return name


def parse_json_object(text: str) -> dict:
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
