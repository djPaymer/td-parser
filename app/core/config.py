from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    """LLM access and the exploration budget used to derive the product URL regex."""

    api_key: str = ""
    base_url: str = "https://ai.api.cloud.yandex.net/v1"
    model: str = "deepseek-v4-flash/latest"
    folder_id: str = ""
    temperature: float = 0.1
    # reasoning models (deepseek) spend hidden tokens before the answer; keep this generous
    max_output_tokens: int = 4000
    # exploration budget: how many pages of the site to download while deriving the regex
    max_pages: int = 16
    # how many URL shapes are offered to the LLM
    max_candidates: int = 10
    # a regex is accepted only if it matches at least this many product links on fetched pages
    min_hits: int = 3


class ParseConfig(BaseModel):
    """Limits for collecting product URLs and downloading product pages per site."""

    # non-product pages (home, categories, listings, pager pages) visited while collecting URLs;
    # a safety net against endless sites, 0 = unlimited
    max_listing_pages: int = 2000
    # pager pages followed for one listing, 0 = unlimited
    max_pages_per_listing: int = 200
    # product pages downloaded per site (each gives description + specs), 0 = all that were found
    max_products: int = 0
    # sites processed in parallel (each with its own HTTP client and per-host delay)
    concurrency: int = 3


BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / "app" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_nested_delimiter="__",
        env_prefix="APP_CONFIG__",
    )
    fetch: FetchConfig = FetchConfig()
    agent: AgentConfig = AgentConfig()
    parse: ParseConfig = ParseConfig()


settings = Settings()
