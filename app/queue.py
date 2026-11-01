"""Things the API (producer) and the worker (consumer) must agree on."""

from arq.connections import RedisSettings

from app.config import Settings

REVIEW_PR_JOB = "review_pr"

# arq cancels a job after this long, so a run still `running` after it was abandoned by a
# worker that died. arq re-runs such a job only after job_timeout + 10 s, by which time the
# run is stale and the new attempt can take it over. Sized for real reviews, which wait on
# Groq's tokens-per-minute limit. Shared by the worker and the reconciler.
JOB_TIMEOUT_SECONDS = 900


def arq_redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(str(settings.redis_url))


def review_job_id(repo_full_name: str, pr_number: int, head_sha: str) -> str:
    """One job per (repo, PR, head SHA). arq refuses to enqueue a second job with the same ID."""
    return f"{repo_full_name}:{pr_number}:{head_sha}"
