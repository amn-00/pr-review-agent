from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file).

    Field names map to env vars case-insensitively: `redis_url` reads REDIS_URL.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")
    # Not used until Phase 1 (DB tables), declared now so compose and .env.example are complete.
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
