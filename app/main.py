import logging

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)

    app = FastAPI(title="PR Review Agent")
    app.include_router(health_router)
    return app


app = create_app()
