import json

import httpx
import pytest
from arq import Retry
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ReviewRun, RunStatus
from app.queue import REVIEW_PR_JOB
from app.worker import (
    BACKOFF_MAX_SECONDS,
    WorkerSettings,
    retry_delay,
    review_pr,
    run_marker,
)
from tests.factories import PR_NUMBER, REPO, SHA_1, SHA_2, pr_payload, pull_response

PULL_URL = f"/repos/{REPO}/pulls/{PR_NUMBER}"
COMMENTS_URL = f"/repos/{REPO}/issues/{PR_NUMBER}/comments"


@pytest.fixture
def ctx(sessionmaker, github):
    return {
        "sessionmaker": sessionmaker,
        "github": github,
        "settings": get_settings(),
        "job_try": 1,
    }


async def get_run(sessionmaker) -> ReviewRun:
    async with sessionmaker() as session:
        return await session.scalar(select(ReviewRun))


def mock_open_pr(github_api, existing_comments=()):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json=pull_response()))
    github_api.get(COMMENTS_URL).mock(
        return_value=httpx.Response(200, json=list(existing_comments))
    )
    return github_api.post(COMMENTS_URL).mock(return_value=httpx.Response(201, json={"id": 99}))


async def test_posts_review_started_comment_with_run_marker(ctx, github_api):
    post = mock_open_pr(github_api)

    result = await review_pr(ctx, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert result == RunStatus.COMPLETED
    assert run.status == RunStatus.COMPLETED
    assert run.github_comment_id == 99
    assert run.finished_at is not None
    body = json.loads(post.calls.last.request.content)["body"]
    assert run_marker(run.id) in body
    assert "Review started" in body


async def test_does_not_post_again_if_marker_already_on_pr(ctx, github_api):
    # Simulates a worker that died after GitHub accepted the comment but before the DB update.
    # Run IDs restart at 1 for each test, so this test's run will have ID 1.
    existing = [{"id": 42, "body": f"{run_marker(1)}\nReview started"}]
    post = mock_open_pr(github_api, existing_comments=existing)

    result = await review_pr(ctx, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert run.id == 1
    assert result == RunStatus.COMPLETED
    assert post.call_count == 0
    assert run.github_comment_id == 42


async def test_completed_run_is_not_processed_again(ctx, github_api):
    post = mock_open_pr(github_api)
    await review_pr(ctx, pr_payload(), "delivery-1")

    result = await review_pr(ctx, pr_payload("reopened"), "delivery-2")

    assert result == RunStatus.COMPLETED
    assert post.call_count == 1
    assert github_api.calls.call_count == 3  # get pull + list comments + post, from run 1 only


@pytest.mark.parametrize(
    "current",
    [
        pull_response(sha=SHA_2),  # a newer commit was pushed
        pull_response(state="closed"),
        pull_response(draft=True),  # converted back to draft
    ],
)
async def test_skips_when_pr_has_moved_on(ctx, github_api, current):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json=current))
    post = github_api.post(COMMENTS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))

    result = await review_pr(ctx, pr_payload(sha=SHA_1), "delivery-1")

    assert result == RunStatus.SKIPPED
    assert post.call_count == 0
    assert (await get_run(ctx["sessionmaker"])).status == RunStatus.SKIPPED


async def test_transient_error_schedules_a_retry(ctx, github_api):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(Retry):
        await review_pr(ctx, pr_payload(), "delivery-1")

    assert (await get_run(ctx["sessionmaker"])).status == RunStatus.RUNNING


async def test_retry_after_transient_error_succeeds(ctx, github_api):
    github_api.get(PULL_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=pull_response())]
    )
    github_api.get(COMMENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    github_api.post(COMMENTS_URL).mock(return_value=httpx.Response(201, json={"id": 99}))

    with pytest.raises(Retry):
        await review_pr(ctx, pr_payload(), "delivery-1")
    result = await review_pr({**ctx, "job_try": 2}, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert result == RunStatus.COMPLETED
    assert run.status == RunStatus.COMPLETED
    assert run.error is None


async def test_gives_up_after_max_tries(ctx, github_api):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(502))
    last_try = ctx["settings"].job_max_tries

    result = await review_pr({**ctx, "job_try": last_try}, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert result == RunStatus.FAILED
    assert run.status == RunStatus.FAILED
    assert "gave up after" in run.error


async def test_permanent_error_fails_without_retry(ctx, github_api):
    github_api.get(PULL_URL).mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )

    result = await review_pr(ctx, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert result == RunStatus.FAILED
    assert "403" in run.error


async def test_unexpected_error_is_recorded_and_raised(ctx, github_api):
    # A response missing fields we rely on: a bug on our side, not a GitHub outage.
    github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))

    with pytest.raises(KeyError):
        await review_pr(ctx, pr_payload(), "delivery-1")

    run = await get_run(ctx["sessionmaker"])
    assert run.status == RunStatus.FAILED
    assert "unexpected error" in run.error


def test_retry_delay_uses_github_retry_after():
    assert retry_delay(job_try=1, retry_after=30) == 31


def test_retry_delay_grows_exponentially_and_is_capped():
    first = retry_delay(job_try=1, retry_after=None)
    third = retry_delay(job_try=3, retry_after=None)
    late = retry_delay(job_try=20, retry_after=None)

    assert 2.5 <= first <= 5
    assert 10 <= third <= 20
    assert BACKOFF_MAX_SECONDS / 2 <= late <= BACKOFF_MAX_SECONDS


def test_worker_registers_review_job_under_shared_name():
    assert [f.name for f in WorkerSettings.functions] == [REVIEW_PR_JOB]
    assert WorkerSettings.max_tries == get_settings().job_max_tries
