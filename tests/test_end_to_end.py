"""Webhook -> arq queue -> a real arq Worker -> Postgres, with only GitHub faked.

These check what unit tests can't: that arq lets the same commit be queued again once its
earlier job has finished (keep_result=0), and that Postgres then decides whether to rerun.
"""

import httpx
from arq.worker import Worker
from sqlalchemy import select

from app.db.models import ReviewRun, RunStatus
from app.worker import WorkerSettings
from tests.factories import PR_NUMBER, REPO, pr_payload, pull_response, send_event

PULL_URL = f"/repos/{REPO}/pulls/{PR_NUMBER}"
COMMENTS_URL = f"/repos/{REPO}/issues/{PR_NUMBER}/comments"


async def run_worker(arq_redis, sessionmaker, github) -> None:
    """Process everything queued, using the real WorkerSettings, then return."""
    worker = Worker(
        functions=WorkerSettings.functions,
        redis_pool=arq_redis,
        burst=True,
        poll_delay=0.01,
        handle_signals=False,
        max_tries=WorkerSettings.max_tries,
        job_timeout=WorkerSettings.job_timeout,
        keep_result=WorkerSettings.keep_result,
        ctx={"sessionmaker": sessionmaker, "github": github},
    )
    # Not worker.close(): it would close the fixture's Redis pool (the fixture does that).
    await worker.main()


async def run_status(sessionmaker) -> str:
    async with sessionmaker() as session:
        return (await session.scalar(select(ReviewRun))).status


def mock_comments(github_api):
    github_api.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    return github_api.post(COMMENTS_URL).mock(return_value=httpx.Response(201, json={"id": 99}))


async def test_close_and_reopen_same_commit_is_reviewed(
    client, arq_redis, sessionmaker, github, github_api
):
    github_api.get(PULL_URL).mock(
        side_effect=[
            httpx.Response(200, json=pull_response(state="closed")),  # closed before the job ran
            httpx.Response(200, json=pull_response()),
        ]
    )
    post = mock_comments(github_api)

    await send_event(client, pr_payload("opened"), delivery="delivery-1")
    await run_worker(arq_redis, sessionmaker, github)
    assert await run_status(sessionmaker) == RunStatus.SKIPPED

    # Reopened minutes later, same head SHA, same job ID.
    response = await send_event(client, pr_payload("reopened"), delivery="delivery-2")
    assert response.status_code == 202  # arq no longer remembers the finished job
    await run_worker(arq_redis, sessionmaker, github)

    assert await run_status(sessionmaker) == RunStatus.COMPLETED
    assert post.call_count == 1


async def test_failed_run_reruns_on_new_delivery(
    client, arq_redis, sessionmaker, github, github_api
):
    github_api.get(PULL_URL).mock(
        side_effect=[
            httpx.Response(403, json={"message": "Resource not accessible"}),
            httpx.Response(200, json=pull_response()),
        ]
    )
    post = mock_comments(github_api)

    await send_event(client, pr_payload("opened"), delivery="delivery-1")
    await run_worker(arq_redis, sessionmaker, github)
    assert await run_status(sessionmaker) == RunStatus.FAILED

    response = await send_event(client, pr_payload("reopened"), delivery="delivery-2")
    assert response.status_code == 202
    await run_worker(arq_redis, sessionmaker, github)

    assert await run_status(sessionmaker) == RunStatus.COMPLETED
    assert post.call_count == 1


async def test_completed_commit_is_not_reviewed_again(
    client, arq_redis, sessionmaker, github, github_api
):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json=pull_response()))
    post = mock_comments(github_api)

    await send_event(client, pr_payload("opened"), delivery="delivery-1")
    await run_worker(arq_redis, sessionmaker, github)

    # arq accepts the job again (it keeps no results), but Postgres says it's done.
    response = await send_event(client, pr_payload("reopened"), delivery="delivery-2")
    assert response.status_code == 202
    await run_worker(arq_redis, sessionmaker, github)

    assert await run_status(sessionmaker) == RunStatus.COMPLETED
    assert post.call_count == 1
