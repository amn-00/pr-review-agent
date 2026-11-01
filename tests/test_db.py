import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Finding, PullRequest, Repo, ReviewRun, RunStatus
from app.db.queries import (
    claim_run,
    finish_run,
    get_or_create_run,
    record_findings,
    release_run,
)
from app.github.webhooks import PullRequestPayload
from app.pipeline.lanes import Candidate
from tests.factories import SHA_1, SHA_2, pr_payload

STALE_AFTER = 300


def payload(**kwargs) -> PullRequestPayload:
    return PullRequestPayload.model_validate(pr_payload(**kwargs))


async def set_run(session, run_id: int, **values) -> None:
    await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(**values))
    await session.commit()


async def count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_same_commit_returns_the_same_run(sessionmaker):
    async with sessionmaker() as session:
        first = await get_or_create_run(session, payload())
        second = await get_or_create_run(session, payload(action="reopened"))

        assert first.id == second.id
        assert await count(session, ReviewRun) == 1


async def test_new_commit_creates_a_new_run_for_the_same_pr(sessionmaker):
    async with sessionmaker() as session:
        first = await get_or_create_run(session, payload(sha=SHA_1))
        second = await get_or_create_run(session, payload(sha=SHA_2))

        assert first.id != second.id
        assert first.pr_id == second.pr_id
        assert await count(session, PullRequest) == 1


async def test_pr_title_is_updated_on_later_events(sessionmaker):
    async with sessionmaker() as session:
        await get_or_create_run(session, payload(title="Old title"))
        await get_or_create_run(session, payload(sha=SHA_2, title="New title"))

        pr = await session.scalar(select(PullRequest))
        assert pr.title == "New title"


async def test_renamed_repo_updates_the_existing_row(sessionmaker):
    renamed = pr_payload()
    renamed["repository"]["full_name"] = "octo/renamed"

    async with sessionmaker() as session:
        await get_or_create_run(session, payload())
        await get_or_create_run(session, PullRequestPayload.model_validate(renamed))

        assert await count(session, Repo) == 1
        assert (await session.scalar(select(Repo))).full_name == "octo/renamed"


async def test_new_run_starts_queued(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())

        assert run.status == RunStatus.QUEUED


@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.FAILED, RunStatus.SKIPPED])
async def test_unfinished_runs_can_be_claimed(sessionmaker, status):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        await set_run(session, run.id, status=status, error="earlier error")

        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is True

        await session.refresh(run)
        assert run.status == RunStatus.RUNNING
        assert run.error is None


async def test_completed_run_cannot_be_claimed(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        await finish_run(session, run.id, RunStatus.COMPLETED)

        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is False


async def test_running_run_cannot_be_claimed_while_fresh(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is True

        # A second worker arrives while the first is still within the job timeout.
        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is False


async def test_stale_running_run_is_taken_over(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        abandoned_at = datetime.now(UTC) - timedelta(seconds=STALE_AFTER + 1)
        await set_run(session, run.id, status=RunStatus.RUNNING, started_at=abandoned_at)

        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is True

        await session.refresh(run)
        assert run.started_at > abandoned_at  # the new attempt's start time


async def test_two_workers_racing_only_one_claims(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())

    async def claim() -> bool:
        async with sessionmaker() as session:  # separate connection per "worker"
            return await claim_run(session, run.id, stale_after_seconds=STALE_AFTER)

    results = await asyncio.gather(claim(), claim())

    assert sorted(results) == [False, True]


async def test_released_run_can_be_claimed_by_the_retry(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        await claim_run(session, run.id, stale_after_seconds=STALE_AFTER)
        await release_run(session, run.id)

        assert await claim_run(session, run.id, stale_after_seconds=STALE_AFTER) is True


async def test_database_rejects_unknown_status(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())

        with pytest.raises(IntegrityError):
            await finish_run(session, run.id, "exploded")


def candidate(title: str, drop_reason: str | None = None) -> Candidate:
    return Candidate(
        lane="correctness",
        prompt_version="v1",
        file="a.py",
        line_start=3,
        line_end=3,
        category="bug",
        severity="high",
        title=title,
        explanation="e",
        suggested_fix=None,
        drop_reason=drop_reason,
    )


async def test_record_findings_replaces_earlier_attempts_and_marks_posted(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        await record_findings(session, run.id, [candidate("first try")], True, 10, 1)

        # A retry records again: the first attempt's findings must not linger.
        kept, dup = candidate("kept"), candidate("dup", drop_reason="duplicate")
        await record_findings(session, run.id, [kept, dup], True, 300, 60)

        rows = list(await session.scalars(select(Finding).order_by(Finding.id)))
        assert [(r.title, r.posted, r.drop_reason) for r in rows] == [
            ("kept", True, None),
            ("dup", False, "duplicate"),
        ]
        await session.refresh(run)
        assert (run.tokens_in, run.tokens_out) == (300, 60)
