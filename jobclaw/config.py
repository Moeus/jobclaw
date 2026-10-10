"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / ".jobclaw"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore",
    )

    jobclaw_headless: bool = False
    jobclaw_request_timeout: int = Field(default=30, ge=5, le=300)
    search_queries: str = ""
    resume_path: str | None = None

    # Any OpenAI-compatible Chat Completions endpoint. Defaults suit Qwen/DashScope.
    openai_api_key: str | None = None
    openai_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    openai_model: str = "qwen-plus"

    boss_city: str = "100010000"
    boss_job_type: str = "all"
    boss_browser_user_data_dir: Path = STATE_DIR / "browser" / "boss"
    boss_browser_port: int = Field(default=9334, ge=1024, le=65535)
    boss_browser_path: str | None = None
    boss_apply_delay_min: float = Field(default=3.0, ge=0.5)
    boss_apply_delay_max: float = Field(default=8.0, ge=1.0)
    boss_daily_limit: int = Field(default=100, ge=1, le=150)

    # Disabled means salary is not used to filter cards or write greetings.
    salary_filter_enabled: bool = False
    min_annual_salary: int | None = Field(default=None, ge=0)

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    discord_webhook_url: str | None = None
    feishu_webhook_url: str | None = None
    feishu_webhook_secret: str | None = None

    @property
    def queries(self) -> list[str]:
        return list(dict.fromkeys(q.strip() for q in self.search_queries.split(",") if q.strip()))

    @model_validator(mode="after")
    def validate_limits(self) -> "Settings":
        if self.boss_apply_delay_max < self.boss_apply_delay_min:
            raise ValueError("BOSS_APPLY_DELAY_MAX must be >= BOSS_APPLY_DELAY_MIN")
        if self.min_annual_salary is not None and not self.salary_filter_enabled:
            raise ValueError("Set SALARY_FILTER_ENABLED=true to use MIN_ANNUAL_SALARY")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
