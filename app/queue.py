"""Things the API (producer) and the worker (consumer) must agree on."""

from arq.connections import RedisSettings

from app.config import Settings

REVIEW_PR_JOB = "review_pr"


def arq_redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(str(settings.redis_url))


def review_job_id(repo_full_name: str, pr_number: int, head_sha: str) -> str:
    """One job per (repo, PR, head SHA). arq refuses to enqueue a second job with the same ID."""
    return f"{repo_full_name}:{pr_number}:{head_sha}"
