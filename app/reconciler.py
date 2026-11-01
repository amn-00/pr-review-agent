"""The safety net for missed webhooks and lost jobs.

Render's free tier sleeps and GitHub doesn't retry failed deliveries, and Redis on that tier
loses its data on restart. Every few minutes (and at worker startup) this lists open PRs from
GitHub and enqueues any recent head SHA that has no finished review. Webhooks stay the fast
path; enqueueing uses the same job ID, so arq ignores PRs that are already queued.
"""

import logging
from datetime import UTC, datetime, timedelta

from app.db.models import ReviewRun, RunStatus
from app.db.queries import find_run
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
    # completed: done. failed: only a new delivery retries it, so a broken PR doesn't
    # burn the token budget every few minutes.
    return False


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
                logger.info(
                    "enqueued a review the webhook path missed",
                    extra={
                        "fields": {
                            "repo": repo["full_name"],
                            "pr": pull["number"],
                            "sha": pull["head"]["sha"],
                            "run_status": run.status if run else None,
                        }
                    },
                )
    return enqueued
