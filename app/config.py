from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """The only setting migrations need, so `alembic upgrade` runs without GitHub secrets.

    Field names map to env vars case-insensitively: `database_url` reads DATABASE_URL.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review"
    )


class Settings(DatabaseSettings):
    """Everything the API and worker need. All of it comes from env vars (or .env)."""

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")

    # GitHub App. No defaults: the app must not start without them.
    github_app_id: str
    # PEM contents. Escaped "\n" sequences are allowed so the key fits on one line in .env.
    github_app_private_key: SecretStr
    github_webhook_secret: SecretStr

    def github_private_key_pem(self) -> str:
        return self.github_app_private_key.get_secret_value().replace("\\n", "\n")


@lru_cache
def get_settings() -> Settings:
    return Settings()
