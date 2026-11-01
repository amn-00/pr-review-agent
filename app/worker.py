"""arq worker. Start it with: arq app.worker.WorkerSettings"""

import logging
import random
import time
from datetime import UTC, datetime, timedelta

import httpx
from arq import Retry, cron, func

from app.config import get_settings
from app.db.models import RunStatus
from app.db.queries import claim_run, finish_run, get_or_create_run, record_findings, release_run
from app.db.session import make_engine, make_sessionmaker
from app.github.client import GitHubClient
from app.github.diff import Hunk, parse_patch
from app.github.http import NonRetryableError, RetryableError
from app.github.webhooks import PullRequestPayload
from app.llm.groq import GroqClient
from app.log import bind_log_context, set_log_context, setup_logging
from app.pipeline.aggregate import aggregate
from app.pipeline.chunking import Chunk, build_chunks, skip_reason
from app.pipeline.lanes import Candidate, lane_models
from app.pipeline.orchestrator import nothing_reviewed_for_daily_budget, run_lanes
from app.pipeline.review import inline_comment, summary
from app.queue import JOB_TIMEOUT_SECONDS, REVIEW_PR_JOB, arq_redis_settings
from app.reconciler import RECONCILE_EVERY_MINUTES, RECONCILE_TIMEOUT_SECONDS, reconcile
from app.static.collect import attach_hints, collect_tool_findings

logger = logging.getLogger(__name__)
settings = get_settings()

JOB_MAX_TRIES = 5
# The LLM work must end by this many seconds after the job starts; chunks not reached are
# listed as skipped. The rest of the timeout covers fetching, posting and GitHub retries.
REVIEW_DEADLINE_SECONDS = 600
# Two reviews at once already use up Groq's tokens-per-minute limit; more would just queue
# inside the worker and use memory.
MAX_JOBS = 2
# A deferred job runs this long after midnight UTC, when the daily budget has reset.
DAILY_RESET_MARGIN_SECONDS = 300
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
    """Review one commit of a PR and post the result as one GitHub review."""
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
    deadline = time.monotonic() + REVIEW_DEADLINE_SECONDS

    try:
        status, review_id = await _review(ctx, payload, run.id, deadline)
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

    await _finish(ctx, run.id, status, github_review_id=review_id)
    logger.info("job finished", extra={"fields": {"status": status}})
    return status


async def _review(
    ctx: dict, payload: PullRequestPayload, run_id: int, deadline: float
) -> tuple[RunStatus, int | None]:
    github: GitHubClient = ctx["github"]
    installation_id = payload.installation.id
    repo = payload.repository.full_name
    number = payload.pull_request.number
    sha = payload.pull_request.head.sha

    # The webhook may be stale by the time the job runs; GitHub has the current state.
    if await _moved_on(github, installation_id, repo, number, sha):
        return RunStatus.SKIPPED, None

    # Before any LLM spend: did an earlier attempt already post (and then die)?
    marker = run_marker(run_id)
    posted = await _find_our_review(github, installation_id, repo, number, marker)
    if posted:
        logger.info("review already posted by an earlier try")
        return RunStatus.COMPLETED, posted["id"]

    chunks, hunks_by_file, skipped_files = await _load_chunks(
        github, installation_id, repo, number, sha
    )
    models = lane_models(settings.groq_model, settings.groq_small_model)
    lanes = await run_lanes(
        ctx["groq"], ctx["sessionmaker"], models, payload.repository.id, run_id, chunks, deadline
    )
    if nothing_reviewed_for_daily_budget(lanes):
        # Nothing worth posting: wait for the budget to reset instead of posting an empty review.
        raise RetryableError(
            "daily token budget used up; reviewing after it resets at midnight UTC",
            retry_after=seconds_until_utc_midnight() + DAILY_RESET_MARGIN_SECONDS,
        )
    candidates = aggregate(lanes.candidates, hunks_by_file)
    to_post = [c for c in candidates if c.drop_reason is None]

    # A new commit may have arrived while the lanes ran; don't post a stale review.
    if await _moved_on(github, installation_id, repo, number, sha):
        await _record(ctx, run_id, candidates, False)
        return RunStatus.SKIPPED, None

    body = summary(marker, sha, models, to_post, list(hunks_by_file), skipped_files, lanes)
    comments = [inline_comment(c) for c in to_post]
    review = await github.create_review(installation_id, repo, number, sha, body, comments)
    await _record(ctx, run_id, candidates, True)
    logger.info("review posted", extra={"fields": {"comments": len(comments)}})
    return RunStatus.COMPLETED, review["id"]


async def _moved_on(
    github: GitHubClient, installation_id: int, repo: str, number: int, sha: str
) -> bool:
    pull = await github.get_pull(installation_id, repo, number)
    moved_on = pull["state"] != "open" or pull["draft"] or pull["head"]["sha"] != sha
    if moved_on:
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
    return moved_on


async def _find_our_review(
    github: GitHubClient, installation_id: int, repo: str, number: int, marker: str
) -> dict | None:
    """Only reviews authored by our App's bot account count. Anyone can paste a marker into
    their own review; only our App can post as "<slug>[bot]"."""
    bot = await github.bot_login()
    for review in await github.list_reviews(installation_id, repo, number):
        author = (review.get("user") or {}).get("login")
        if author == bot and marker in (review.get("body") or ""):
            return review
    return None


async def _load_chunks(
    github: GitHubClient, installation_id: int, repo: str, number: int, sha: str
) -> tuple[list[Chunk], dict[str, list[Hunk]], dict[str, str]]:
    """Returns the chunks to review, the hunks of each reviewed file (for the hallucination
    guard), and the files not reviewed with the reason."""
    chunks: list[Chunk] = []
    hunks_by_file: dict[str, list[Hunk]] = {}
    skipped_files: dict[str, str] = {}
    contents: dict[str, str] = {}
    for file in await github.list_pull_files(installation_id, repo, number):
        path = file["filename"]
        reason = skip_reason(file)
        if reason:
            skipped_files[path] = reason
            continue
        hunks = parse_patch(file["patch"])
        if not hunks:  # only deletions: nothing on the new side to comment on
            continue
        content = await github.get_file_content(installation_id, repo, path, sha)
        hunks_by_file[path] = hunks
        contents[path] = content
        chunks.extend(build_chunks(path, hunks, content))

    # Static tools: findings on added lines become hints for the matching lane.
    added_lines = {
        path: set().union(*(h.added for h in hunks)) for path, hunks in hunks_by_file.items()
    }
    tool_findings = await collect_tool_findings(contents, added_lines, settings.semgrep_enabled)
    attach_hints(chunks, tool_findings)
    return chunks, hunks_by_file, skipped_files


async def _record(ctx: dict, run_id: int, candidates: list[Candidate], posted: bool) -> None:
    async with ctx["sessionmaker"]() as session:
        await record_findings(session, run_id, candidates, posted)


def seconds_until_utc_midnight(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


async def _finish(ctx: dict, run_id: int, status: RunStatus, **fields) -> None:
    async with ctx["sessionmaker"]() as session:
        await finish_run(session, run_id, status, **fields)


async def startup(ctx: dict) -> None:
    setup_logging(settings.log_level)
    engine = make_engine(str(settings.database_url))
    ctx["engine"] = engine
    ctx["sessionmaker"] = make_sessionmaker(engine)
    ctx["github"] = GitHubClient.from_settings(settings)
    ctx["groq"] = GroqClient(httpx.AsyncClient(), settings.groq_api_key.get_secret_value())


async def shutdown(ctx: dict) -> None:
    await ctx["github"].aclose()
    await ctx["groq"].aclose()
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [func(review_pr, name=REVIEW_PR_JOB)]
    cron_jobs = [
        # At startup too: on Render, the request that wakes the service may be the very
        # webhook that was lost. max_tries=1 (arq's default): the next cycle is the retry.
        cron(
            reconcile,
            minute=set(range(0, 60, RECONCILE_EVERY_MINUTES)),
            run_at_startup=True,
            timeout=RECONCILE_TIMEOUT_SECONDS,
        )
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = arq_redis_settings(settings)
    max_tries = JOB_MAX_TRIES
    job_timeout = JOB_TIMEOUT_SECONDS
    max_jobs = MAX_JOBS
    # Don't keep finished jobs' results. arq refuses to enqueue a job ID whose result still
    # exists, which would block re-reviewing a commit (e.g. after close + reopen) for as long
    # as the result is kept. Postgres's unique (pr_id, head_sha) is the durable dedupe instead.
    keep_result = 0
