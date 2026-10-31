from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.db.session import make_engine
from app.log import setup_logging
from app.queue import arq_redis_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    app.state.arq = await create_pool(arq_redis_settings(settings))
    app.state.engine = make_engine(str(settings.database_url))
    yield
    await app.state.arq.aclose()
    await app.state.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="PR Review Agent", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(webhook_router)
    return app


app = create_app()
