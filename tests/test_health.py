import httpx
from fastapi.testclient import TestClient

from app.db.session import make_engine
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL


def test_health_returns_ok():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_rejects_wrong_method():
    client = TestClient(create_app())

    response = client.post("/health")

    assert response.status_code == 405


def test_root_redirects_to_the_repo():
    client = TestClient(create_app())

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://github.com/amn-00/pr-review-agent"


def test_unknown_route_returns_404():
    client = TestClient(create_app())

    response = client.get("/does-not-exist")

    assert response.status_code == 404


class BrokenRedis:
    async def ping(self):
        raise ConnectionError("redis is down")


async def get_ready(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health/ready")


async def test_ready_when_redis_and_postgres_are_up(arq_redis, migrated_database):
    app = create_app()
    app.state.arq = arq_redis
    app.state.engine = make_engine(TEST_DATABASE_URL)

    response = await get_ready(app)
    await app.state.engine.dispose()

    assert response.status_code == 200
    assert response.json() == {"redis": "ok", "postgres": "ok"}


async def test_not_ready_when_redis_is_down(migrated_database):
    app = create_app()
    app.state.arq = BrokenRedis()
    app.state.engine = make_engine(TEST_DATABASE_URL)

    response = await get_ready(app)
    await app.state.engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"redis": "error", "postgres": "ok"}


async def test_not_ready_when_postgres_is_down(arq_redis):
    app = create_app()
    app.state.arq = arq_redis
    # Nothing listens on port 1, so connecting fails fast.
    app.state.engine = make_engine("postgresql+asyncpg://postgres:postgres@localhost:1/none")

    response = await get_ready(app)
    await app.state.engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"redis": "ok", "postgres": "error"}
