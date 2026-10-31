from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file).

    Field names map to env vars case-insensitively: `redis_url` reads REDIS_URL.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    redis_url: RedisDsn = RedisDsn("redis://localhost:6379/0")
    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review"
    )

    # GitHub App. No defaults: the app must not start without them.
    github_app_id: str
    # PEM contents. Escaped "\n" sequences are allowed so the key fits on one line in .env.
    github_app_private_key: SecretStr
    github_webhook_secret: SecretStr
    github_api_url: str = "https://api.github.com"
    # Stop calling GitHub (and defer the job) when fewer requests than this remain.
    github_min_rate_limit_remaining: int = 50

    # How long a webhook delivery ID is remembered for dedupe. GitHub allows redelivery
    # of events up to 3 days old, so remembering them for 3 days covers manual redeliveries.
    delivery_ttl_seconds: int = 3 * 24 * 3600
    job_max_tries: int = 5

    def github_private_key_pem(self) -> str:
        return self.github_app_private_key.get_secret_value().replace("\\n", "\n")


@lru_cache
def get_settings() -> Settings:
    return Settings()
