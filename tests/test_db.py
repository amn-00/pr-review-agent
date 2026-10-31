import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import PullRequest, Repo, ReviewRun, RunStatus
from app.db.queries import finish_run, get_or_create_run, mark_running
from app.github.webhooks import PullRequestPayload
from tests.factories import SHA_1, SHA_2, pr_payload


def payload(**kwargs) -> PullRequestPayload:
    return PullRequestPayload.model_validate(pr_payload(**kwargs))


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


async def test_finished_run_can_be_reset_to_running(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())
        await finish_run(session, run.id, RunStatus.FAILED, error="boom")
        await mark_running(session, run.id)

        await session.refresh(run)
        assert run.status == RunStatus.RUNNING
        assert run.error is None


async def test_database_rejects_unknown_status(sessionmaker):
    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload())

        with pytest.raises(IntegrityError):
            await finish_run(session, run.id, "exploded")
