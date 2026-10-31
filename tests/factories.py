"""Builders for test data and requests shaped like GitHub's."""

import hashlib
import hmac
import json

import httpx

from tests.conftest import WEBHOOK_SECRET

SHA_1 = "a" * 40
SHA_2 = "b" * 40
REPO = "octo/hello"
PR_NUMBER = 7
INSTALLATION_ID = 555


def pr_payload(
    action: str = "opened", sha: str = SHA_1, draft: bool = False, title: str = "Add feature"
) -> dict:
    """A pull_request webhook payload, trimmed to the fields we read plus a few we ignore."""
    return {
        "action": action,
        "number": PR_NUMBER,
        "installation": {"id": INSTALLATION_ID, "node_id": "ignored"},
        "repository": {
            "id": 1001,
            "full_name": REPO,
            "private": False,
            "owner": {"login": "octo", "id": 1},
        },
        "pull_request": {
            "number": PR_NUMBER,
            "title": title,
            "draft": draft,
            "state": "open",
            "head": {"sha": sha, "ref": "feature"},
            "user": {"login": "alice"},
        },
        "sender": {"login": "alice"},
    }


def pull_response(sha: str = SHA_1, state: str = "open", draft: bool = False) -> dict:
    """GitHub's GET /repos/{repo}/pulls/{n} response, trimmed."""
    return {"number": PR_NUMBER, "state": state, "draft": draft, "head": {"sha": sha}}


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def send_event(
    client: httpx.AsyncClient,
    payload: dict,
    *,
    delivery: str | None = "delivery-1",
    event: str = "pull_request",
    signature: str | None = None,
) -> httpx.Response:
    """POST a webhook the way GitHub does: raw JSON body, signed, with event/delivery headers."""
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature if signature is not None else sign(body),
        "Content-Type": "application/json",
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return await client.post("/webhook", content=body, headers=headers)
