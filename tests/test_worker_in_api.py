"""The arq worker inside the API process (RUN_WORKER_IN_API), as it runs on Render's free tier.

These run the real lifespan, the real WorkerSettings and a real arq Worker. Only GitHub and Groq
are faked, and the worker's startup is swapped for one that hands it the test fixtures.
"""

import asyncio
import logging

import httpx
import pytest
from sqlalchemy import select

import app.main
from app.config import get_settings
from app.db.models import ReviewRun, RunStatus
from app.main import create_app, lifespan
from app.queue import REVIEW_PR_JOB
from app.worker import WorkerSettings
from tests.factories import mock_github_pr, pr_payload, send_event
from tests.test_end_to_end import run_worker


@pytest.fixture
def worker_in_api(monkeypatch, sessionmaker, github, groq, github_api):
    """Turn the setting on, and give the in-process worker the test clients."""

    async def startup(ctx):
        ctx.update(sessionmaker=sessionmaker, github=github, groq=groq)

    async def shutdown(ctx):
        pass  # the fixtures close their own clients

    monkeypatch.setattr(get_settings(), "run_worker_in_api", True)
    monkeypatch.setattr(WorkerSettings, "on_startup", startup)
    monkeypatch.setattr(WorkerSettings, "on_shutdown", shutdown)
    # The lifespan would replace pytest's log handlers for the rest of the session.
    monkeypatch.setattr(app.main, "setup_logging", lambda level: None)
    # The reconciler runs at worker startup: no installations, so it finds nothing to do.
    github_api.get("/app/installations").mock(return_value=httpx.Response(200, json=[]))


def api_client(api) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")


async def wait_for_status(sessionmaker, status: str) -> None:
    async with asyncio.timeout(15):
        while True:
            async with sessionmaker() as session:
                run = await session.scalar(select(ReviewRun))
            if run is not None and run.status == status:
                return
            await asyncio.sleep(0.1)


async def run_status(sessionmaker) -> str:
    async with sessionmaker() as session:
        return (await session.scalar(select(ReviewRun))).status


async def test_webhook_is_reviewed_by_the_worker_in_the_api_process(
    worker_in_api, arq_redis, sessionmaker, github_api
):
    post = mock_github_pr(github_api)
    api = create_app()

    async with lifespan(api), api_client(api) as client:
        response = await send_event(client, pr_payload("opened"))
        assert response.status_code == 202
        await wait_for_status(sessionmaker, RunStatus.COMPLETED)
        assert (await client.get("/health")).status_code == 200

    assert post.call_count == 1


async def test_no_worker_when_the_setting_is_off(monkeypatch, arq_redis, migrated_database):
    monkeypatch.setattr(app.main, "setup_logging", lambda level: None)
    assert get_settings().run_worker_in_api is False  # the default
    api = create_app()

    async with lifespan(api), api_client(api) as client:
        assert api.state.worker_task is None
        assert (await client.get("/health")).status_code == 200


async def test_shutdown_hands_an_in_flight_run_back_and_the_next_start_posts_once(
    worker_in_api, arq_redis, sessionmaker, github, groq, github_api
):
    """Render sends SIGTERM on a deploy and on spin-down; uvicorn then runs the lifespan's
    shutdown. The in-flight run must be `queued` and its job back in Redis."""
    post = mock_github_pr(github_api)
    lanes = groq.complete_json
    in_lanes = asyncio.Event()

    async def hang(*args, **kwargs):
        in_lanes.set()
        await asyncio.Event().wait()

    groq.complete_json = hang
    api = create_app()
    # The timeout covers the shutdown too: one that doesn't cancel the job would wait forever.
    async with asyncio.timeout(15):
        async with lifespan(api), api_client(api) as client:
            await send_event(client, pr_payload("opened"))
            await in_lanes.wait()

    assert await run_status(sessionmaker) == RunStatus.QUEUED
    review_jobs = [job for job in await arq_redis.queued_jobs() if job.function == REVIEW_PR_JOB]
    assert len(review_jobs) == 1

    groq.complete_json = lanes
    await run_worker(arq_redis, sessionmaker, github, groq)  # the next start
    assert await run_status(sessionmaker) == RunStatus.COMPLETED
    assert post.call_count == 1


async def test_a_crashed_worker_fails_the_health_check(
    worker_in_api, monkeypatch, arq_redis, caplog
):
    async def broken_startup(ctx):
        raise RuntimeError("startup bug")

    monkeypatch.setattr(WorkerSettings, "on_startup", broken_startup)
    api = create_app()

    with caplog.at_level(logging.ERROR, logger="app.main"):
        async with lifespan(api), api_client(api) as client:
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(api.state.worker_task, 15)
            response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "worker stopped"}
    assert "in-process worker stopped" in caplog.text
