import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_VARS = [
    "ENVIRONMENT",
    "LOG_LEVEL",
    "REDIS_URL",
    "DATABASE_URL",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
]
REQUIRED = {
    "github_app_id": "1",
    "github_app_private_key": "key",
    "github_webhook_secret": "secret",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Make tests independent of the developer's shell, .env file and conftest's test env.
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})


def test_defaults_are_local_dev():
    settings = make_settings()

    assert settings.environment == "dev"
    assert settings.log_level == "INFO"
    assert str(settings.redis_url) == "redis://localhost:6379/0"
    assert settings.github_api_url == "https://api.github.com"


def test_reads_values_from_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("REDIS_URL", "rediss://user:pass@example.com:6380/0")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "from-env")

    settings = Settings(_env_file=None, github_app_id="1", github_app_private_key="key")

    assert settings.environment == "prod"
    assert settings.redis_url.scheme == "rediss"
    assert settings.redis_url.host == "example.com"
    assert settings.github_webhook_secret.get_secret_value() == "from-env"


def test_secrets_are_hidden_when_printed():
    settings = make_settings(github_webhook_secret="hunter2")

    assert "hunter2" not in repr(settings)


def test_private_key_newlines_are_unescaped():
    settings = make_settings(github_app_private_key="-----BEGIN-----\\nabc\\n-----END-----")

    assert settings.github_private_key_pem() == "-----BEGIN-----\nabc\n-----END-----"


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_rejects_missing_github_settings(missing):
    values = {k: v for k, v in REQUIRED.items() if k != missing}

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_rejects_unknown_environment():
    with pytest.raises(ValidationError):
        make_settings(environment="staging")


def test_rejects_non_redis_url():
    with pytest.raises(ValidationError):
        make_settings(redis_url="http://localhost:6379")


def test_rejects_non_postgres_database_url():
    with pytest.raises(ValidationError):
        make_settings(database_url="mysql://localhost/db")
