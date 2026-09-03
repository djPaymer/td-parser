from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class RunConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class ApiV1Prefix(BaseModel):
    prefix: str = "/v1"


class ApiPrefix(BaseModel):
    prefix: str = "/api"
    v1: ApiV1Prefix = ApiV1Prefix()


class FetchConfig(BaseModel):
    timeout: float = 45
    delay_seconds: float = 0.35
    max_bytes: int = 500_000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    verify_ssl: bool = False


class AgentConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://ai.api.cloud.yandex.net/v1"
    model: str = "deepseek-v4-flash/latest"
    folder_id: str = ""
    max_turns: int = 8
    max_pages: int = 12
    html_limit: int = 40_000
    max_output_tokens: int = 1500


BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / "app" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        env_prefix="APP_CONFIG__",
    )
    run: RunConfig = RunConfig()
    api: ApiPrefix = ApiPrefix()
    fetch: FetchConfig = FetchConfig()
    agent: AgentConfig = AgentConfig()


settings = Settings()
