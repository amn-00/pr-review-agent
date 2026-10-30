"""arq worker. Start it with: arq app.worker.WorkerSettings"""

import logging

from arq.connections import RedisSettings

from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level)


async def ping(ctx: dict) -> str:
    """Placeholder job so the worker has something registered. Real jobs arrive in Phase 1."""
    return "pong"


class WorkerSettings:
    functions = [ping]
    redis_settings = RedisSettings.from_dsn(str(settings.redis_url))
