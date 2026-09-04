from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    model_id: str = Field(default="gemini-3.0-pro", alias="OPENAI_MODEL")
    cors_origins_raw: str = Field(default="http://localhost:5173", alias="CORS_ORIGINS")
    database_path: str = Field(default=str(ROOT_DIR / "backend" / "data" / "stock_agent.db"), alias="STOCK_DATABASE_PATH")

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.cors_origins_raw.split(",") if value.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
