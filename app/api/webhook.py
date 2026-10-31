import logging
from typing import Annotated

from arq import ArqRedis
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.github.webhooks import REVIEW_ACTIONS, PullRequestPayload, verify_signature
from app.log import bind_log_context, set_log_context
from app.queue import REVIEW_PR_JOB, review_job_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _respond(status_code: int, status: str) -> JSONResponse:
    return JSONResponse({"status": status}, status_code=status_code)


@router.post("/webhook")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Validate, filter and enqueue. Must stay fast: all real work happens in the worker."""
    settings = get_settings()
    set_log_context(delivery_id=x_github_delivery, event=x_github_event)

    body = await request.body()
    secret = settings.github_webhook_secret.get_secret_value()
    if not verify_signature(secret, body, x_hub_signature_256):
        logger.warning("rejected webhook: invalid signature")
        return _respond(401, "invalid signature")
    if not x_github_delivery:
        return _respond(400, "missing delivery id")
    if x_github_event != "pull_request":
        return _respond(200, "ignored")

    try:
        payload = PullRequestPayload.model_validate_json(body)
    except ValidationError:
        logger.warning("rejected webhook: unexpected pull_request payload")
        return _respond(400, "invalid payload")

    pr = payload.pull_request
    repo = payload.repository.full_name
    bind_log_context(repo=repo, pr=pr.number, sha=pr.head.sha, action=payload.action)
    if payload.action not in REVIEW_ACTIONS:
        logger.info("ignored pull_request action")
        return _respond(200, "ignored")
    if pr.draft:
        logger.info("ignored draft PR")
        return _respond(200, "ignored")

    arq: ArqRedis = request.app.state.arq
    delivery_key = f"delivery:{x_github_delivery}"
    claimed = False
    try:
        # First filter: an exact replay of a delivery we've already accepted.
        claimed = bool(await arq.set(delivery_key, "1", nx=True, ex=settings.delivery_ttl_seconds))
        if not claimed:
            logger.info("duplicate delivery, not enqueued")
            return _respond(200, "duplicate")
        # Real dedupe: arq returns None if a job with this ID is already queued or finished.
        job = await arq.enqueue_job(
            REVIEW_PR_JOB,
            payload.model_dump(),
            x_github_delivery,
            _job_id=review_job_id(repo, pr.number, pr.head.sha),
        )
    except RedisError:
        logger.exception("could not enqueue review job")
        if claimed:
            # Release the delivery ID so GitHub's redelivery (same ID) isn't treated as a duplicate.
            await _forget_delivery(arq, delivery_key)
        return _respond(503, "queue unavailable")

    if job is None:
        logger.info("review for this commit already queued or done")
        return _respond(200, "duplicate")
    logger.info("review job enqueued")
    return _respond(202, "queued")


async def _forget_delivery(arq: ArqRedis, delivery_key: str) -> None:
    try:
        await arq.delete(delivery_key)
    except RedisError:
        logger.exception("could not release delivery id")
