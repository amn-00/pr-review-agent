import asyncio
import os

import asyncpg
import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.engine import make_url

# Tests use their own database and Redis DB 1, so they never touch dev data and a locally
# running worker (Redis DB 0) never picks up test jobs.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/1")
WEBHOOK_SECRET = "test-webhook-secret"
GITHUB_API = "https://api.github.com"

# Must run before any app module is imported: app.main and app.worker read settings at import.
# Overwrite (not setdefault) so a DATABASE_URL in the shell can't point tests at a real database.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_PRIVATE_KEY": "not-used-in-tests",
        "GITHUB_WEBHOOK_SECRET": WEBHOOK_SECRET,
    }
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from arq import create_pool  # noqa: E402
from arq.connections import RedisSettings  # noqa: E402

from app.db.session import make_engine, make_sessionmaker  # noqa: E402
from app.github.client import GitHubClient  # noqa: E402

TABLES = "review_runs, pull_requests, repos, installations"


async def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
    admin_dsn = target.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin_dsn.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_database() -> None:
    """Create the test database and run the real migrations once per test session,
    so the migrations themselves are tested too."""
    asyncio.run(_create_database_if_missing(TEST_DATABASE_URL))
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
async def sessionmaker(migrated_database):
    engine = make_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def arq_redis():
    pool = await create_pool(RedisSettings.from_dsn(TEST_REDIS_URL))
    await pool.flushdb()
    yield pool
    await pool.flushdb()
    await pool.aclose()


@pytest.fixture
def github_api():
    """Fake GitHub API. Any request without a matching route fails the test."""
    with respx.mock(base_url=GITHUB_API, assert_all_called=False) as mock:
        yield mock


class StubAuth:
    """Stands in for GitHubAppAuth in tests that aren't about authentication."""

    async def installation_token(self, installation_id: int) -> str:
        return "test-token"


@pytest.fixture
async def github():
    client = GitHubClient(
        httpx.AsyncClient(base_url=GITHUB_API), StubAuth(), min_rate_limit_remaining=50
    )
    yield client
    await client.aclose()
