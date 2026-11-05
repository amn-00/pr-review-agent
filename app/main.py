import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.worker import Worker, create_worker
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.db.session import make_engine
from app.log import setup_logging
from app.queue import arq_redis_settings
from app.worker import WorkerSettings

logger = logging.getLogger(__name__)


def start_worker() -> tuple[Worker, asyncio.Task]:
    """Run the prod arq worker as a task in this process.

    handle_signals=False: uvicorn owns SIGTERM and runs the lifespan shutdown, which stops the
    worker. Two signal handlers would fight over it.
    """
    worker = create_worker(WorkerSettings, handle_signals=False)
    worker.main_task = asyncio.create_task(worker.main())
    worker.main_task.add_done_callback(_log_if_crashed)
    return worker, worker.main_task


def _log_if_crashed(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        # /health now answers 503, so the platform restarts the process.
        logger.error("in-process worker stopped", exc_info=task.exception())


async def stop_worker(worker: Worker, task: asyncio.Task) -> None:
    """Stop the worker as arq does on SIGTERM: running jobs are cancelled, so they hand their
    runs back as `queued`, and arq puts them back in the queue for the next start.

    Not Worker.close(): with handle_signals=False it calls handle_sig(signal.SIGUSR1), which
    doesn't exist on Windows. These are its steps, minus deleting arq's health-check key,
    which expires by itself.
    """
    if not task.done():
        worker.handle_sig(signal.SIGTERM)
        await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await worker.on_shutdown(worker.ctx)
    # A crashed worker skips on_shutdown: its startup may not have finished, and the process is
    # about to be restarted anyway.
    if worker.pool is not None:
        await worker.pool.aclose(close_connection_pool=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    app.state.arq = await create_pool(arq_redis_settings(settings))
    app.state.engine = make_engine(str(settings.database_url))
    app.state.worker_task = None
    worker = None
    if settings.run_worker_in_api:
        worker, app.state.worker_task = start_worker()
    yield
    if worker is not None:
        await stop_worker(worker, app.state.worker_task)
    await app.state.arq.aclose()
    await app.state.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="PR Review Agent", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(webhook_router)
    return app


app = create_app()
