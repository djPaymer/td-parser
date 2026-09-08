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
    # catalog pages with inline mega-menus / base64 images reach 1 MB; truncating them hides the products
    max_bytes: int = 3_000_000
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
    temperature: float = 0.1
    # reasoning models (deepseek) spend hidden tokens before the answer; keep this generous
    max_output_tokens: int = 4000
    # exploration budget: how many pages of the site to download while building an instruction
    max_pages: int = 16
    # how many URL shapes are offered to the LLM
    max_candidates: int = 10
    # an instruction is accepted only if it matches at least this many product links on fetched pages
    min_hits: int = 3


class DbConfig(BaseModel):
    # PostgreSQL DSN, e.g. postgresql://service:service@localhost:5432/parser
    # Empty string disables instruction persistence
    url: str = ""


class StoreConfig(BaseModel):
    # POST /instruction returns the stored instruction when it is younger than this (0 = always rebuild)
    ttl_days: int = 30


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
    db: DbConfig = DbConfig()
    store: StoreConfig = StoreConfig()


settings = Settings()
