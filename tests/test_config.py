import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_VARS = ["ENVIRONMENT", "LOG_LEVEL", "REDIS_URL", "DATABASE_URL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Make tests independent of the developer's shell and .env file.
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings() -> Settings:
    return Settings(_env_file=None)


def test_defaults_are_local_dev():
    settings = make_settings()

    assert settings.environment == "dev"
    assert settings.log_level == "INFO"
    assert str(settings.redis_url) == "redis://localhost:6379/0"


def test_reads_values_from_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("REDIS_URL", "rediss://user:pass@example.com:6380/0")

    settings = make_settings()

    assert settings.environment == "prod"
    assert settings.redis_url.scheme == "rediss"
    assert settings.redis_url.host == "example.com"


def test_rejects_unknown_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        make_settings()


def test_rejects_non_redis_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "http://localhost:6379")

    with pytest.raises(ValidationError):
        make_settings()


def test_rejects_non_postgres_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://localhost/db")

    with pytest.raises(ValidationError):
        make_settings()
