"""The load test's job wiring, webhook sender, scenarios and metrics."""

import httpx
import pytest
from arq.worker import get_kwargs
from sqlalchemy import select

from app.db.models import Finding, ReviewRun, RunStatus
from app.main import create_app
from app.worker import WorkerSettings, review_pr
from loadtest.fakes import create_app as create_fakes_app
from loadtest.report import build_report, metrics, percentile
from loadtest.run import send_all
from loadtest.scenarios import MIX, mixed
from loadtest.worker import LoadTestWorkerSettings, fake_clients, startup
from tests.conftest import WEBHOOK_SECRET

FAKES_URL = "http://fakes:9000"


@pytest.fixture
async def fakes():
    """The fakes app, and an admin client for it."""
    app = create_fakes_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=FAKES_URL) as admin:
        yield app, admin


async def test_one_pr_through_the_real_job_posts_one_confirmed_comment(sessionmaker, fakes):
    app, admin = fakes
    [event] = mixed(1, "e2e")
    pull = {k: getattr(event, k) for k in ("installation_id", "repo_id", "repo", "number", "sha")}
    (await admin.post("/_admin/pulls", json=pull)).raise_for_status()
    github, groq = fake_clients(FAKES_URL, "worker-1", httpx.ASGITransport(app=app))
    ctx = {"sessionmaker": sessionmaker, "github": github, "groq": groq}

    try:
        status = await review_pr(ctx, event.payload(), event.delivery)
    finally:
        await github.aclose()
        await groq.aclose()

    assert status == RunStatus.COMPLETED
    [review] = (await admin.get("/_admin/records")).json()["reviews"]
    assert (review["sha"], review["comments"], review["stale"]) == (event.sha, 1, False)
    async with sessionmaker() as session:
        run = await session.scalar(select(ReviewRun))
        [finding] = (await session.scalars(select(Finding))).all()
    assert run.agent_enabled
    assert (finding.verdict, finding.posted) == ("confirmed", True)


async def test_every_webhook_gets_its_expected_status(arq_redis, fakes):
    _, admin = fakes
    api_app = create_app()
    api_app.state.arq = arq_redis  # the lifespan doesn't run under ASGITransport
    events = mixed(80, "statuses", seed=3)

    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as api:
        records = await send_all(api, admin, events, WEBHOOK_SECRET, concurrency=10)

    wrong = [(r["kind"], r["status"]) for r in records if r["status"] not in r["expected"]]
    assert wrong == []
    progress = (await admin.get("/_admin/progress")).json()
    assert progress["heads"] == sum(e.kind == "opened" for e in events)


def test_load_test_worker_keeps_every_prod_setting_but_startup():
    # arq reads only the class's own __dict__: a plain subclass would run with no functions.
    load_test = get_kwargs(LoadTestWorkerSettings)
    prod = get_kwargs(WorkerSettings)

    assert load_test.pop("on_startup") is startup
    assert prod.pop("on_startup") is not startup
    assert load_test == prod
    assert {"functions", "cron_jobs", "max_jobs", "job_timeout", "keep_result"} <= set(prod)


def test_mixed_is_deterministic_and_consistent():
    events = mixed(300, "n", seed=7)

    assert [vars(e) for e in events] == [vars(e) for e in mixed(300, "n", seed=7)]
    assert {e.kind for e in events} == set(MIX)
    for i, event in enumerate(events):
        if event.after is not None:
            assert event.after < i
            target = events[event.after]
            assert (target.repo, target.number) == (event.repo, event.number)
        if event.kind == "replay":
            assert event.delivery == events[event.after].delivery and not event.push
        if event.kind == "synchronize":
            assert event.push and event.sha != events[event.after].sha
        if event.kind == "same_head":
            assert event.sha == events[event.after].sha and not event.push
        if event.kind == "ignored":
            assert not event.push and event.expected == (200,)
    fresh = [e.delivery for e in events if e.kind != "replay"]
    assert len(fresh) == len(set(fresh))


def test_percentile_is_nearest_rank():
    values = list(range(1, 21))
    assert (percentile(values, 50), percentile(values, 95), percentile(values, 100)) == (10, 19, 20)
    assert percentile([], 95) is None


def _webhook(kind: str, sha: str, t: float, status: int, expected=(202,)) -> dict:
    return {
        "kind": kind,
        "sha": sha,
        "t": t,
        "status": status,
        "expected": list(expected),
        "latency_ms": 10.0,
    }


def _review(sha: str, t: float, stale: bool = False) -> dict:
    return {"repo": "o/r", "number": 1, "sha": sha, "t": t, "stale": stale, "comments": 1}


def test_metrics_count_duplicates_lost_and_stale_and_time_from_the_first_202():
    result = {
        "name": "t",
        "webhooks": [
            _webhook("opened", "a", 100.0, 202),
            _webhook("replay", "a", 101.0, 200, expected=(200,)),
            _webhook("opened", "b", 102.0, 503),  # unexpected
            _webhook("opened", "c", 103.0, 202),
        ],
        "records": {
            "pulls": [
                {"head": "a", "state": "open", "draft": False},
                {"head": "b", "state": "open", "draft": False},  # never reviewed: lost
                {"head": "c", "state": "open", "draft": False},
            ],
            "reviews": [_review("a", 110.0), _review("a", 130.0), _review("c", 104.0, stale=True)],
            "first_seen": {"a": {"t": 105.0, "worker": "w"}},
            "github_requests": {"1": 30},
            "github_errors": {},
            "groq_calls": {},
        },
        "memory_peak_mib": {},
    }

    m = metrics(result)

    assert (m["duplicates"], m["lost"], m["stale_posts"]) == (1, 1, 1)
    assert m["review_time_s"]["max"] == 10.0  # a: sent at 100, first review at 110
    assert (m["queue_wait_s"]["p50"], m["work_time_s"]["p50"]) == (5.0, 5.0)
    assert m["unexpected_statuses"] == {"opened -> 503": 1}
    assert m["github_requests_per_review"] == 10.0


def test_report_has_a_row_per_run():
    result = {
        "name": "L1",
        "config": {},
        "environment": {"workers": 1},
        "git_sha": "abc",
        "settled": True,
        "webhooks": [_webhook("opened", "a", 1.0, 202)],
        "records": {
            "pulls": [],
            "reviews": [_review("a", 2.0)],
            "first_seen": {},
            "github_requests": {},
            "github_errors": {},
            "groq_calls": {},
        },
        "memory_peak_mib": {},
    }

    report = build_report([result])

    assert "| L1 | 1 | 1 |" in report
    assert "### L1" in report
