import hashlib
import hmac
import json

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.main import create_app
from tests.conftest import WEBHOOK_SECRET
from tests.factories import PR_NUMBER, REPO, SHA_1, SHA_2, pr_payload


@pytest.fixture
async def client(arq_redis):
    app = create_app()
    # The lifespan doesn't run under ASGITransport, so wire the Redis pool by hand.
    app.state.arq = arq_redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_event(
    client,
    payload: dict,
    *,
    delivery: str | None = "delivery-1",
    event: str = "pull_request",
    signature: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature if signature is not None else sign(body),
        "Content-Type": "application/json",
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return await client.post("/webhook", content=body, headers=headers)


async def queued_jobs(arq_redis) -> list:
    return await arq_redis.queued_jobs()


async def test_valid_pr_event_is_queued(client, arq_redis):
    response = await send_event(client, pr_payload())

    assert response.status_code == 202
    jobs = await queued_jobs(arq_redis)
    assert len(jobs) == 1
    assert jobs[0].function == "review_pr"
    assert jobs[0].job_id == f"{REPO}:{PR_NUMBER}:{SHA_1}"
    assert jobs[0].args[1] == "delivery-1"


async def test_replayed_delivery_is_not_queued_twice(client, arq_redis):
    first = await send_event(client, pr_payload(), delivery="delivery-1")
    replay = await send_event(client, pr_payload(), delivery="delivery-1")

    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json() == {"status": "duplicate"}
    assert len(await queued_jobs(arq_redis)) == 1


async def test_new_delivery_for_same_commit_is_not_queued_twice(client, arq_redis):
    # e.g. "opened" and "reopened" for the same head SHA: different delivery IDs.
    await send_event(client, pr_payload("opened"), delivery="delivery-1")
    second = await send_event(client, pr_payload("reopened"), delivery="delivery-2")

    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(await queued_jobs(arq_redis)) == 1


async def test_new_commit_gets_its_own_job(client, arq_redis):
    await send_event(client, pr_payload("opened", sha=SHA_1), delivery="delivery-1")
    await send_event(client, pr_payload("synchronize", sha=SHA_2), delivery="delivery-2")

    assert len(await queued_jobs(arq_redis)) == 2


async def test_ready_for_review_is_queued(client, arq_redis):
    response = await send_event(client, pr_payload("ready_for_review"))

    assert response.status_code == 202
    assert len(await queued_jobs(arq_redis)) == 1


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, "md5=abc"])
async def test_bad_signature_is_rejected(client, arq_redis, signature):
    response = await send_event(client, pr_payload(), signature=signature)

    assert response.status_code == 401
    assert await queued_jobs(arq_redis) == []


async def test_signature_from_wrong_secret_is_rejected(client, arq_redis):
    body = json.dumps(pr_payload()).encode()
    response = await send_event(client, pr_payload(), signature=sign(body, "wrong-secret"))

    assert response.status_code == 401


async def test_missing_signature_header_is_rejected(client, arq_redis):
    response = await client.post(
        "/webhook",
        content=b"{}",
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d"},
    )

    assert response.status_code == 401


async def test_other_events_are_ignored(client, arq_redis):
    response = await send_event(client, {"zen": "Keep it simple."}, event="ping")

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await queued_jobs(arq_redis) == []


@pytest.mark.parametrize("action", ["closed", "edited", "labeled", "converted_to_draft"])
async def test_other_pr_actions_are_ignored(client, arq_redis, action):
    response = await send_event(client, pr_payload(action))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await queued_jobs(arq_redis) == []


async def test_draft_prs_are_ignored(client, arq_redis):
    response = await send_event(client, pr_payload("opened", draft=True))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await queued_jobs(arq_redis) == []


async def test_malformed_payload_is_rejected(client, arq_redis):
    response = await send_event(client, {"action": "opened", "pull_request": {"number": 1}})

    assert response.status_code == 400
    assert await queued_jobs(arq_redis) == []


async def test_missing_delivery_id_is_rejected(client, arq_redis):
    response = await send_event(client, pr_payload(), delivery=None)

    assert response.status_code == 400


async def test_enqueue_failure_returns_503_and_releases_delivery_id(client, arq_redis, monkeypatch):
    async def broken_enqueue(*args, **kwargs):
        raise RedisConnectionError("redis went away")

    with monkeypatch.context() as patch:
        patch.setattr(arq_redis, "enqueue_job", broken_enqueue)
        response = await send_event(client, pr_payload(), delivery="delivery-1")

    assert response.status_code == 503
    assert await arq_redis.exists("delivery:delivery-1") == 0

    # GitHub's redelivery reuses the delivery ID; it must not be treated as a duplicate.
    redelivery = await send_event(client, pr_payload(), delivery="delivery-1")
    assert redelivery.status_code == 202
    assert len(await queued_jobs(arq_redis)) == 1
