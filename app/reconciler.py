"""The safety net for missed webhooks and lost jobs.

Render's free tier sleeps and GitHub doesn't retry failed deliveries, and Redis on that tier
loses its data on restart. Every few minutes (and at worker startup) this lists open PRs from
GitHub and enqueues any recent head SHA that has no finished review. Webhooks stay the fast
path; enqueueing uses the same job ID, so arq ignores PRs that are already queued.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.db.models import FailureKind, ReviewRun, RunStatus
from app.db.queries import find_run, take_auto_retry
from app.github.client import GitHubClient
from app.github.http import NonRetryableError, RetryableError
from app.github.webhooks import PullRequestPayload
from app.log import set_log_context
from app.queue import JOB_TIMEOUT_SECONDS, REVIEW_PR_JOB, review_job_id

logger = logging.getLogger(__name__)

RECONCILE_EVERY_MINUTES = 5
RECONCILE_TIMEOUT_SECONDS = 120
# Only PRs updated this recently. A missed webhook always comes from a recent push or open,
# which bumps `updated_at`; older PRs would spend the token budget on reviews nobody asked for.
LOOKBACK = timedelta(hours=24)
# Stands in for the webhook delivery ID in the job's logs, so reconciler work is easy to find.
RECONCILER_DELIVERY_ID = "reconciler"
# A run that failed on a transient error (it used up its tries during a GitHub outage, say) is
# retried this many times, 5, 10 and then 20 minutes after each failure. In Phase 6 G2 every job
# had given up within ~1.5 minutes of an outage starting; three retries cover ~40 minutes.
MAX_AUTO_RETRIES = 3
AUTO_RETRY_FIRST_DELAY = timedelta(minutes=5)


def should_enqueue(run: ReviewRun | None, now: datetime) -> bool:
    """Does this PR's head SHA need a (new) job?"""
    if run is None:
        return True  # webhook missed
    if run.status == RunStatus.QUEUED:
        return True  # its job may have been lost with Redis; arq ignores it if still queued
    if run.status == RunStatus.SKIPPED:
        return True  # was closed or a draft when its job ran; open again at the same head
    if run.status == RunStatus.RUNNING:
        return run.started_at < now - timedelta(seconds=JOB_TIMEOUT_SECONDS)  # worker died
    if run.status == RunStatus.FAILED:
        return auto_retry_due(run, now)
    return False  # completed: done


def auto_retry_due(run: ReviewRun, now: datetime) -> bool:
    """A transient failure with retries left, failed long enough ago. Permanent failures are
    only retried by a new delivery, so a broken PR doesn't burn the token budget."""
    if run.failure_kind != FailureKind.TRANSIENT or run.auto_retries >= MAX_AUTO_RETRIES:
        return False
    delay = AUTO_RETRY_FIRST_DELAY * 2**run.auto_retries
    return run.finished_at is not None and run.finished_at <= now - delay


async def reconcile(ctx: dict) -> int:
    """arq cron job. Returns how many jobs it enqueued."""
    set_log_context(delivery_id=RECONCILER_DELIVERY_ID)
    github: GitHubClient = ctx["github"]
    now = datetime.now(UTC)
    enqueued = 0
    for installation in await github.list_installations():
        if installation["suspended_at"]:
            continue
        try:
            enqueued += await _reconcile_installation(ctx, installation["id"], now)
        except (RetryableError, NonRetryableError) as exc:
            # One broken installation mustn't stop the others; the next cycle tries again.
            logger.warning("reconcile failed for installation %s: %s", installation["id"], exc)
    logger.info("reconcile finished", extra={"fields": {"enqueued": enqueued}})
    return enqueued


async def _reconcile_installation(ctx: dict, installation_id: int, now: datetime) -> int:
    github: GitHubClient = ctx["github"]
    enqueued = 0
    for repo in await github.list_installation_repos(installation_id):
        pulls = await github.list_open_pulls(installation_id, repo["full_name"], now - LOOKBACK)
        for pull in pulls:
            if pull["draft"]:
                continue
            async with ctx["sessionmaker"]() as session:
                run = await find_run(session, repo["id"], pull["number"], pull["head"]["sha"])
            if not should_enqueue(run, now):
                continue
            # The same shape the webhook enqueues; pydantic keeps only the fields we use.
            payload = PullRequestPayload.model_validate(
                {
                    "action": "reconcile",
                    "installation": {"id": installation_id},
                    "repository": repo,
                    "pull_request": pull,
                }
            )
            job = await ctx["redis"].enqueue_job(
                REVIEW_PR_JOB,
                payload.model_dump(),
                RECONCILER_DELIVERY_ID,
                _job_id=review_job_id(repo["full_name"], pull["number"], pull["head"]["sha"]),
            )
            if job is not None:
                enqueued += 1
                if run and run.status == RunStatus.FAILED:
                    # Counted only once arq accepted the job: a retry whose job is still queued
                    # from the last cycle isn't used up, and arq's job ID lets only one cycle in.
                    async with ctx["sessionmaker"]() as session:
                        await take_auto_retry(session, run.id, run.auto_retries)
                logger.info(
                    "enqueued a review the webhook path missed",
                    extra={
                        "fields": {
                            "repo": repo["full_name"],
                            "pr": pull["number"],
                            "sha": pull["head"]["sha"],
                            "run_status": run.status if run else None,
                            "auto_retry": run.auto_retries + 1
                            if run and run.status == RunStatus.FAILED
                            else None,
                        }
                    },
                )
    return enqueued
