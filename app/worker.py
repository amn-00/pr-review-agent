"""arq worker. Start it with: arq app.worker.WorkerSettings"""

import logging
import random

from arq import Retry, func

from app.config import get_settings
from app.db.models import RunStatus
from app.db.queries import claim_run, finish_run, get_or_create_run, release_run
from app.db.session import make_engine, make_sessionmaker
from app.github.client import GitHubClient
from app.github.http import NonRetryableError, RetryableError
from app.github.webhooks import PullRequestPayload
from app.log import bind_log_context, set_log_context, setup_logging
from app.queue import REVIEW_PR_JOB, arq_redis_settings

logger = logging.getLogger(__name__)
settings = get_settings()

JOB_MAX_TRIES = 5
# arq cancels a job after this long, so a run still `running` after it was abandoned by a
# worker that died. arq re-runs such a job only after job_timeout + 10 s, by which time the
# run is stale and the new attempt can take it over.
JOB_TIMEOUT_SECONDS = 300
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 300


def run_marker(run_id: int) -> str:
    """Hidden HTML comment in everything we post for a run. If a worker dies after GitHub
    accepted the post but before we saved that in the DB, the retry finds this marker on
    the PR and doesn't post again."""
    return f"<!-- pr-review-agent run:{run_id} -->"


def retry_delay(job_try: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, unless GitHub told us how long to wait."""
    if retry_after is not None:
        return retry_after + 1
    backoff = min(BACKOFF_BASE_SECONDS * 2 ** (job_try - 1), BACKOFF_MAX_SECONDS)
    # Jitter spreads out retries so jobs that failed together don't all retry together.
    return random.uniform(backoff / 2, backoff)


async def review_pr(ctx: dict, event: dict, delivery_id: str) -> str:
    """Phase 1: record the run and post a "review started" comment on the PR."""
    payload = PullRequestPayload.model_validate(event)
    pr = payload.pull_request
    set_log_context(
        delivery_id=delivery_id, repo=payload.repository.full_name, pr=pr.number, sha=pr.head.sha
    )
    sessionmaker = ctx["sessionmaker"]
    job_try = ctx.get("job_try", 1)

    async with sessionmaker() as session:
        run = await get_or_create_run(session, payload)
        bind_log_context(run_id=run.id)
        # Only a `completed` run, or one a live worker is on, stops this attempt.
        if not await claim_run(session, run.id, stale_after_seconds=JOB_TIMEOUT_SECONDS):
            logger.info(
                "run is completed or in progress elsewhere, nothing to do",
                extra={"fields": {"run_status": run.status}},
            )
            return run.status
    logger.info("job started", extra={"fields": {"job_try": job_try}})

    try:
        status, comment_id = await _post_placeholder(ctx["github"], payload, run.id)
    except RetryableError as exc:
        if job_try >= JOB_MAX_TRIES:
            error = f"gave up after {job_try} tries: {exc}"
            logger.error(error)
            await _finish(ctx, run.id, RunStatus.FAILED, error=error)
            return RunStatus.FAILED
        delay = retry_delay(job_try, exc.retry_after)
        logger.warning(
            "transient error, will retry: %s",
            exc,
            extra={"fields": {"job_try": job_try, "retry_in_s": round(delay, 1)}},
        )
        async with sessionmaker() as session:
            await release_run(session, run.id)
        raise Retry(defer=delay) from exc
    except NonRetryableError as exc:
        logger.error("permanent error, not retrying: %s", exc)
        await _finish(ctx, run.id, RunStatus.FAILED, error=str(exc))
        return RunStatus.FAILED
    except Exception as exc:
        # A bug, not a GitHub problem: record it, then let arq log the traceback.
        await _finish(ctx, run.id, RunStatus.FAILED, error=f"unexpected error: {exc!r}")
        raise

    await _finish(ctx, run.id, status, github_comment_id=comment_id)
    logger.info("job finished", extra={"fields": {"status": status}})
    return status


async def _post_placeholder(
    github: GitHubClient, payload: PullRequestPayload, run_id: int
) -> tuple[RunStatus, int | None]:
    installation_id = payload.installation.id
    repo = payload.repository.full_name
    number = payload.pull_request.number
    sha = payload.pull_request.head.sha

    # The webhook may be stale by the time the job runs; GitHub has the current state.
    pull = await github.get_pull(installation_id, repo, number)
    if pull["state"] != "open" or pull["draft"] or pull["head"]["sha"] != sha:
        logger.info(
            "PR moved on, skipping",
            extra={
                "fields": {
                    "state": pull["state"],
                    "draft": pull["draft"],
                    "current_sha": pull["head"]["sha"],
                }
            },
        )
        return RunStatus.SKIPPED, None

    marker = run_marker(run_id)
    for comment in await github.list_issue_comments(installation_id, repo, number):
        if marker in (comment.get("body") or ""):
            logger.info("comment already posted by an earlier try")
            return RunStatus.COMPLETED, comment["id"]

    body = f"{marker}\nReview started for commit `{sha[:7]}`."
    comment = await github.create_issue_comment(installation_id, repo, number, body)
    return RunStatus.COMPLETED, comment["id"]


async def _finish(ctx: dict, run_id: int, status: RunStatus, **fields) -> None:
    async with ctx["sessionmaker"]() as session:
        await finish_run(session, run_id, status, **fields)


async def startup(ctx: dict) -> None:
    setup_logging(settings.log_level)
    engine = make_engine(str(settings.database_url))
    ctx["engine"] = engine
    ctx["sessionmaker"] = make_sessionmaker(engine)
    ctx["github"] = GitHubClient.from_settings(settings)


async def shutdown(ctx: dict) -> None:
    await ctx["github"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [func(review_pr, name=REVIEW_PR_JOB)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = arq_redis_settings(settings)
    max_tries = JOB_MAX_TRIES
    job_timeout = JOB_TIMEOUT_SECONDS
    # Don't keep finished jobs' results. arq refuses to enqueue a job ID whose result still
    # exists, which would block re-reviewing a commit (e.g. after close + reopen) for as long
    # as the result is kept. Postgres's unique (pr_id, head_sha) is the durable dedupe instead.
    keep_result = 0
