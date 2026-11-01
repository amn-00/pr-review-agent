"""Builders for test data and requests shaped like GitHub's."""

import hashlib
import hmac
import json

import httpx

from app.github.diff import Hunk
from app.pipeline.chunking import Chunk, build_chunks
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


# A PR that changes one file and plants a bug: average() divides by len(values), which
# crashes on an empty list. Line 3 is the added line.
BUGGY_FILE = "app/stats.py"
BUGGY_CONTENT = "def average(values):\n    total = sum(values)\n    return total / len(values)\n"
BUGGY_PATCH = (
    "@@ -1,2 +1,3 @@\n def average(values):\n     total = sum(values)\n"
    "+    return total / len(values)"
)
BOT_LOGIN = "test-app[bot]"


def lane_finding(line_start: int = 3, line_end: int = 3, **overrides) -> dict:
    """A finding as a lane's model returns it (the JSON schema shape)."""
    return {
        "line_start": line_start,
        "line_end": line_end,
        "category": "bug",
        "severity": "high",
        "title": "Division by zero when values is empty",
        "explanation": "len(values) is 0 for an empty list, so this raises ZeroDivisionError.",
        "suggested_fix": "Return 0 (or raise a clear error) when values is empty.",
        **overrides,
    }


def mock_github_pr(
    github_api,
    files: list[dict] | None = None,
    contents: dict[str, str] | None = None,
    reviews: list[dict] | None = None,
):
    """Mock everything a review job reads from GitHub. Returns the create-review route."""
    files = files or [{"filename": BUGGY_FILE, "status": "modified", "patch": BUGGY_PATCH}]
    contents = contents or {BUGGY_FILE: BUGGY_CONTENT}
    pulls = f"/repos/{REPO}/pulls/{PR_NUMBER}"
    github_api.get(pulls).mock(return_value=httpx.Response(200, json=pull_response()))
    github_api.get("/app").mock(return_value=httpx.Response(200, json={"slug": "test-app"}))
    github_api.get(f"{pulls}/reviews").mock(return_value=httpx.Response(200, json=reviews or []))
    github_api.get(f"{pulls}/files").mock(return_value=httpx.Response(200, json=files))
    for path, text in contents.items():
        github_api.get(f"/repos/{REPO}/contents/{path}").mock(
            return_value=httpx.Response(200, text=text)
        )
    return github_api.post(f"{pulls}/reviews").mock(
        return_value=httpx.Response(200, json={"id": 777})
    )


def make_chunk(
    path: str = BUGGY_FILE, content: str = BUGGY_CONTENT, added: set[int] | None = None
) -> Chunk:
    """One chunk covering the whole (short) file; by default its last line is the added one."""
    lines = content.splitlines()
    added = {len(lines)} if added is None else added
    [chunk] = build_chunks(path, [Hunk(1, len(lines), added)], content)
    return chunk
