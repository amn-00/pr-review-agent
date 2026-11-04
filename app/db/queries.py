from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentStep as AgentStepRecord
from app.db.models import (
    AgentStep,
    BudgetPurpose,
    ChunkCache,
    DailyTokenUsage,
    FailureKind,
    Finding,
    Installation,
    PullRequest,
    Repo,
    ReviewRun,
    RunStatus,
)
from app.github.webhooks import PullRequestPayload
from app.pipeline.lanes import Candidate
from app.pipeline.usage import Usage


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
        .values(
            status=RunStatus.RUNNING,
            started_at=func.now(),
            error=None,
            failure_kind=None,
            finished_at=None,
        )
        .returning(ReviewRun.id)
    )
    await session.commit()
    return claimed_id is not None


async def find_run(
    session: AsyncSession, github_repo_id: int, pr_number: int, head_sha: str
) -> ReviewRun | None:
    """The run for one commit of a PR, if any. Every join and filter is covered by an
    existing unique index."""
    return await session.scalar(
        select(ReviewRun)
        .join(PullRequest, ReviewRun.pr_id == PullRequest.id)
        .join(Repo, PullRequest.repo_id == Repo.id)
        .where(
            Repo.github_repo_id == github_repo_id,
            PullRequest.number == pr_number,
            ReviewRun.head_sha == head_sha,
        )
    )


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
    failure_kind: FailureKind | None = None,
    github_review_id: int | None = None,
) -> None:
    await session.execute(
        update(ReviewRun)
        .where(ReviewRun.id == run_id)
        .values(
            status=status,
            error=error,
            failure_kind=failure_kind,
            github_review_id=github_review_id,
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def take_auto_retry(session: AsyncSession, run_id: int, seen_retries: int) -> bool:
    """Count one automatic retry of a failed run. False if it was already counted (the check
    and the increment are one statement)."""
    taken = await session.scalar(
        update(ReviewRun)
        .where(
            ReviewRun.id == run_id,
            ReviewRun.status == RunStatus.FAILED,
            ReviewRun.auto_retries == seen_retries,
        )
        .values(auto_retries=ReviewRun.auto_retries + 1)
        .returning(ReviewRun.id)
    )
    await session.commit()
    return taken is not None


async def record_findings(
    session: AsyncSession,
    run_id: int,
    candidates: list[Candidate],
    agent_enabled: bool,
) -> list[int]:
    """Replace the run's findings (and their agent steps) in one transaction, none posted
    yet. Replacing (not appending) means a retry or a stale-run takeover never leaves
    duplicates behind. Also records whether the agent checks this run. Returns the new
    findings' IDs, in the same order as `candidates`."""
    await session.execute(delete(Finding).where(Finding.run_id == run_id))
    rows = [
        Finding(
            run_id=run_id,
            lane=c.lane,
            prompt_version=c.prompt_version,
            file=c.file,
            line_start=c.line_start,
            line_end=c.line_end,
            category=c.category,
            severity=c.severity,
            title=c.title,
            explanation=c.explanation,
            suggested_fix=c.suggested_fix,
            drop_reason=c.drop_reason,
        )
        for c in candidates
    ]
    session.add_all(rows)
    await session.execute(
        update(ReviewRun).where(ReviewRun.id == run_id).values(agent_enabled=agent_enabled)
    )
    await session.flush()  # assigns the IDs
    # A duplicate's winner is always among `candidates`; match by identity, not by value.
    row_for = {id(c): row for c, row in zip(candidates, rows, strict=True)}
    for c, row in zip(candidates, rows, strict=True):
        if c.duplicate_of is not None:
            row.duplicate_of = row_for[id(c.duplicate_of)].id
    ids = [row.id for row in rows]
    await session.commit()
    return ids


async def set_verdict(
    session: AsyncSession,
    finding_id: int,
    verdict: str | None,
    reason: str,
    evidence: list[dict],
) -> None:
    """Saved as soon as the agent is done with a finding. `verdict` None: not verified."""
    await session.execute(
        update(Finding)
        .where(Finding.id == finding_id)
        .values(verdict=verdict, verdict_reason=reason, evidence=evidence)
    )
    await session.commit()


async def mark_posted(session: AsyncSession, finding_ids: list[int]) -> None:
    await session.execute(update(Finding).where(Finding.id.in_(finding_ids)).values(posted=True))
    await session.commit()


async def add_agent_step(session: AsyncSession, finding_id: int, step: AgentStepRecord) -> None:
    """Written as soon as the step finishes, so a crash keeps the steps already taken."""
    session.add(AgentStep(finding_id=finding_id, **asdict(step)))
    await session.commit()


async def add_run_usage(session: AsyncSession, run_id: int, delta: Usage) -> None:
    """Add one call's usage to the run's totals. Added per call, not written at the end, so
    an attempt that dies or retries still has its spend counted."""
    columns = {name: getattr(ReviewRun, name) + value for name, value in asdict(delta).items()}
    await session.execute(update(ReviewRun).where(ReviewRun.id == run_id).values(**columns))
    await session.commit()


async def get_cached(session: AsyncSession, key: str) -> ChunkCache | None:
    return await session.get(ChunkCache, key)


async def put_cached(
    session: AsyncSession,
    key: str,
    findings: list[dict],
    tokens_in: int,
    tokens_out: int,
    run_id: int,
) -> None:
    """First answer wins: two workers caching the same key store the same kind of result."""
    await session.execute(
        insert(ChunkCache)
        .values(
            key=key, findings=findings, tokens_in=tokens_in, tokens_out=tokens_out, run_id=run_id
        )
        .on_conflict_do_nothing(index_elements=[ChunkCache.key])
    )
    await session.commit()


async def daily_tokens_used(
    session: AsyncSession, model: str, day: date, purpose: BudgetPurpose
) -> int:
    used = await session.scalar(
        select(DailyTokenUsage.tokens).where(
            DailyTokenUsage.day == day,
            DailyTokenUsage.model == model,
            DailyTokenUsage.purpose == purpose,
        )
    )
    return used or 0


async def add_daily_tokens(
    session: AsyncSession, model: str, day: date, purpose: BudgetPurpose, tokens: int
) -> None:
    """Atomic add: concurrent calls can't lose each other's tokens."""
    statement = insert(DailyTokenUsage).values(day=day, model=model, purpose=purpose, tokens=tokens)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[DailyTokenUsage.day, DailyTokenUsage.model, DailyTokenUsage.purpose],
            set_={"tokens": DailyTokenUsage.tokens + statement.excluded.tokens},
        )
    )
    await session.commit()
