import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

logger = logging.getLogger(__name__)
router = APIRouter()

READY_CHECK_TIMEOUT_SECONDS = 2


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness check: the process is up and serving requests. Checks no dependencies."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness check: can we reach Redis (to enqueue) and Postgres?"""
    checks = {
        "redis": await _check(request.app.state.arq.ping()),
        "postgres": await _check(_ping_postgres(request.app.state.engine)),
    }
    ok = all(result == "ok" for result in checks.values())
    return JSONResponse(checks, status_code=200 if ok else 503)


async def _ping_postgres(engine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check(probe) -> str:
    try:
        async with asyncio.timeout(READY_CHECK_TIMEOUT_SECONDS):
            await probe
    except Exception:  # any failure means "not ready"; the reason goes to the log
        logger.exception("readiness check failed")
        return "error"
    return "ok"
