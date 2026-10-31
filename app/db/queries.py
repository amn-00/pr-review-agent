from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Installation, PullRequest, Repo, ReviewRun, RunStatus
from app.github.webhooks import PullRequestPayload


async def get_or_create_run(session: AsyncSession, payload: PullRequestPayload) -> ReviewRun:
    """Upsert the installation, repo and PR from the webhook payload, then return the run
    for this head SHA, creating it if needed.

    Uses INSERT ... ON CONFLICT so two workers racing on the same PR can't create duplicates.
    """
    installation_id = await session.scalar(
        insert(Installation)
        .values(
            github_installation_id=payload.installation.id,
            account=payload.repository.owner.login,
        )
        .on_conflict_do_update(
            index_elements=[Installation.github_installation_id],
            set_={"account": payload.repository.owner.login},
        )
        .returning(Installation.id)
    )
    repo_id = await session.scalar(
        insert(Repo)
        .values(
            installation_id=installation_id,
            github_repo_id=payload.repository.id,
            full_name=payload.repository.full_name,
        )
        .on_conflict_do_update(
            index_elements=[Repo.github_repo_id],
            set_={"installation_id": installation_id, "full_name": payload.repository.full_name},
        )
        .returning(Repo.id)
    )
    pr = payload.pull_request
    pr_id = await session.scalar(
        insert(PullRequest)
        .values(repo_id=repo_id, number=pr.number, title=pr.title, author=pr.user.login)
        .on_conflict_do_update(
            index_elements=[PullRequest.repo_id, PullRequest.number],
            set_={"title": pr.title, "author": pr.user.login},
        )
        .returning(PullRequest.id)
    )
    await session.execute(
        insert(ReviewRun)
        .values(pr_id=pr_id, head_sha=pr.head.sha, status=RunStatus.QUEUED)
        .on_conflict_do_nothing(index_elements=[ReviewRun.pr_id, ReviewRun.head_sha])
    )
    run = await session.scalar(
        select(ReviewRun).where(ReviewRun.pr_id == pr_id, ReviewRun.head_sha == pr.head.sha)
    )
    await session.commit()
    return run


async def claim_run(session: AsyncSession, run_id: int, stale_after_seconds: int) -> bool:
    """Atomically mark the run `running` for this attempt. Returns False if we can't have it.

    Claimable: `queued`, `failed` and `skipped` runs, and `running` runs whose attempt started
    more than `stale_after_seconds` ago (that worker died). Not claimable: `completed` runs and
    runs another worker is actively on. The check and the update are one statement, so two
    workers can't both claim the same run.
    """
    stale_before = func.now() - timedelta(seconds=stale_after_seconds)
    claimed_id = await session.scalar(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            or_(
                ReviewRun.status.in_([RunStatus.QUEUED, RunStatus.FAILED, RunStatus.SKIPPED]),
                and_(ReviewRun.status == RunStatus.RUNNING, ReviewRun.started_at < stale_before),
            ),
        )
        .values(status=RunStatus.RUNNING, started_at=func.now(), error=None, finished_at=None)
        .returning(ReviewRun.id)
    )
    await session.commit()
    return claimed_id is not None


async def release_run(session: AsyncSession, run_id: int) -> None:
    """Hand the run back (status `queued`) while the job waits to retry, so the retry can
    claim it straight away instead of waiting for it to go stale."""
    await session.execute(
        update(ReviewRun).where(ReviewRun.id == run_id).values(status=RunStatus.QUEUED)
    )
    await session.commit()


async def finish_run(
    session: AsyncSession,
    run_id: int,
    status: RunStatus,
    *,
    error: str | None = None,
    github_comment_id: int | None = None,
) -> None:
    await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(
            status=status,
            error=error,
            github_comment_id=github_comment_id,
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()
