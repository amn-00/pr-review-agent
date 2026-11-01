from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.db.models import ReviewRun, RunStatus
from app.db.queries import get_or_create_run
from app.github.webhooks import PullRequestPayload
from app.queue import JOB_TIMEOUT_SECONDS
from app.reconciler import LOOKBACK, RECONCILER_DELIVERY_ID, reconcile, should_enqueue
from tests.factories import INSTALLATION_ID, PR_NUMBER, REPO, SHA_1, pr_payload

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
PULLS_URL = f"/repos/{REPO}/pulls"


def repo_json() -> dict:
    # Same repo as tests.factories.pr_payload, so runs created from that payload match.
    return {"id": 1001, "full_name": REPO, "owner": {"login": "octo"}, "private": False}


def pull_json(number=PR_NUMBER, sha=SHA_1, draft=False, updated_ago=timedelta(hours=1)) -> dict:
    return {
        "number": number,
        "title": "Add feature",
        "draft": draft,
        "state": "open",
        "updated_at": (datetime.now(UTC) - updated_ago).isoformat().replace("+00:00", "Z"),
        "head": {"sha": sha},
        "user": {"login": "alice"},
    }


def mock_github(github_api, pulls=None, installations=None):
    installations = installations or [{"id": INSTALLATION_ID, "suspended_at": None}]
    github_api.get("/app/installations").mock(return_value=httpx.Response(200, json=installations))
    repos = github_api.get("/installation/repositories").mock(
        return_value=httpx.Response(200, json={"total_count": 1, "repositories": [repo_json()]})
    )
    pulls_route = github_api.get(PULLS_URL).mock(
        return_value=httpx.Response(200, json=[pull_json()] if pulls is None else pulls)
    )
    return repos, pulls_route


@pytest.fixture
def ctx(sessionmaker, github, arq_redis):
    return {"sessionmaker": sessionmaker, "github": github, "redis": arq_redis}


async def create_run(ctx, status: RunStatus, started_ago: timedelta = timedelta(0)) -> None:
    async with ctx["sessionmaker"]() as session:
        run = await get_or_create_run(session, PullRequestPayload.model_validate(pr_payload()))
        started_at = datetime.now(UTC) - started_ago
        await session.execute(
            update(ReviewRun)
            .where(ReviewRun.id == run.id)
            .values(status=status, started_at=started_at)
        )
        await session.commit()


@pytest.mark.parametrize(
    ("status", "started_ago", "expected"),
    [
        (RunStatus.QUEUED, 0, True),
        (RunStatus.SKIPPED, 0, True),
        (RunStatus.RUNNING, JOB_TIMEOUT_SECONDS + 1, True),
        (RunStatus.RUNNING, 60, False),
        (RunStatus.COMPLETED, 0, False),
        (RunStatus.FAILED, 0, False),
    ],
)
def test_should_enqueue(status, started_ago, expected):
    run = ReviewRun(status=status, started_at=NOW - timedelta(seconds=started_ago))

    assert should_enqueue(run, NOW) is expected


def test_should_enqueue_when_there_is_no_run():
    assert should_enqueue(None, NOW) is True


async def test_open_pr_without_a_run_gets_a_job(ctx, github_api, arq_redis):
    mock_github(github_api)

    enqueued = await reconcile(ctx)

    assert enqueued == 1
    [job] = await arq_redis.queued_jobs()
    assert job.job_id == f"{REPO}:{PR_NUMBER}:{SHA_1}"
    event, delivery_id = job.args
    assert delivery_id == RECONCILER_DELIVERY_ID
    assert event["action"] == "reconcile"
    assert event["installation"]["id"] == INSTALLATION_ID
    assert event["pull_request"]["head"]["sha"] == SHA_1
    # The payload is the webhook's trimmed shape, so the job can't tell the difference.
    PullRequestPayload.model_validate(event)


@pytest.mark.parametrize(
    ("status", "started_ago"),
    [
        (RunStatus.COMPLETED, timedelta(0)),
        (RunStatus.FAILED, timedelta(0)),
        (RunStatus.RUNNING, timedelta(seconds=60)),  # a worker is on it
    ],
)
async def test_no_job_when_the_run_is_done_failed_or_in_progress(
    ctx, github_api, arq_redis, status, started_ago
):
    mock_github(github_api)
    await create_run(ctx, status, started_ago)

    assert await reconcile(ctx) == 0
    assert await arq_redis.queued_jobs() == []


async def test_queued_run_whose_job_was_lost_is_requeued(ctx, github_api, arq_redis):
    # Redis restarted and lost the queue; Postgres still says `queued`.
    mock_github(github_api)
    await create_run(ctx, RunStatus.QUEUED)

    assert await reconcile(ctx) == 1


async def test_stale_running_run_is_requeued(ctx, github_api, arq_redis):
    mock_github(github_api)
    await create_run(ctx, RunStatus.RUNNING, timedelta(seconds=JOB_TIMEOUT_SECONDS + 60))

    assert await reconcile(ctx) == 1


async def test_job_already_in_the_queue_is_not_duplicated(ctx, github_api, arq_redis):
    mock_github(github_api)

    await reconcile(ctx)
    second = await reconcile(ctx)

    assert second == 0
    assert len(await arq_redis.queued_jobs()) == 1


async def test_drafts_are_skipped(ctx, github_api, arq_redis):
    mock_github(github_api, pulls=[pull_json(draft=True)])

    assert await reconcile(ctx) == 0


async def test_suspended_installation_is_skipped(ctx, github_api):
    suspended = [{"id": INSTALLATION_ID, "suspended_at": "2026-10-01T00:00:00Z"}]
    repos, _ = mock_github(github_api, installations=suspended)

    assert await reconcile(ctx) == 0
    assert repos.call_count == 0


async def test_only_recently_updated_prs_are_reconciled(ctx, github_api, arq_redis):
    recent = pull_json(number=7, updated_ago=timedelta(hours=2))
    old = pull_json(number=8, sha="c" * 40, updated_ago=LOOKBACK + timedelta(hours=1))
    _, pulls = mock_github(github_api)
    next_page = f"https://api.github.com{PULLS_URL}?page=2"
    older_still = pull_json(number=9, sha="d" * 40, updated_ago=LOOKBACK + timedelta(days=30))
    pulls.mock(
        side_effect=[
            httpx.Response(200, json=[recent, old], headers={"Link": f'<{next_page}>; rel="next"'}),
            httpx.Response(200, json=[older_still]),
        ]
    )

    assert await reconcile(ctx) == 1
    # Newest first, and paging stopped at the first old PR: page 2 was never requested.
    assert pulls.call_count == 1
    params = pulls.calls.last.request.url.params
    assert (params["state"], params["sort"], params["direction"]) == ("open", "updated", "desc")


async def test_one_failing_installation_does_not_stop_the_others(ctx, github_api, arq_redis):
    installations = [{"id": 1, "suspended_at": None}, {"id": INSTALLATION_ID, "suspended_at": None}]
    repos, _ = mock_github(github_api, installations=installations)
    repos.mock(
        side_effect=[
            httpx.Response(500),  # installation 1: GitHub error
            httpx.Response(200, json={"total_count": 1, "repositories": [repo_json()]}),
        ]
    )

    assert await reconcile(ctx) == 1
