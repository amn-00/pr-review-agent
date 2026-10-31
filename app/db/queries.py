from datetime import UTC, datetime

from sqlalchemy import select, update
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
        .values(pr_id=pr_id, head_sha=pr.head.sha, status=RunStatus.RUNNING)
        .on_conflict_do_nothing(index_elements=[ReviewRun.pr_id, ReviewRun.head_sha])
    )
    run = await session.scalar(
        select(ReviewRun).where(ReviewRun.pr_id == pr_id, ReviewRun.head_sha == pr.head.sha)
    )
    await session.commit()
    return run


async def mark_running(session: AsyncSession, run_id: int) -> None:
    """Reset a run that is being (re)tried: a failed or skipped run can be picked up again."""
    await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(status=RunStatus.RUNNING, error=None, finished_at=None)
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
