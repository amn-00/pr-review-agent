import hashlib
import hmac

from pydantic import BaseModel

# pull_request actions that should trigger a review. ready_for_review fires when a draft
# is marked ready; drafts themselves are skipped.
REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check GitHub's X-Hub-Signature-256 header: "sha256=" + HMAC-SHA256(secret, raw body)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Constant-time compare, so response timing doesn't leak how much of the signature matched.
    return hmac.compare_digest(expected, signature_header)


# Only the fields we use. Pydantic ignores the rest of GitHub's (large) payload, and this
# small model is what gets queued, which keeps jobs small in Redis.


class _Installation(BaseModel):
    id: int


class _Owner(BaseModel):
    login: str


class _Repository(BaseModel):
    id: int
    full_name: str
    owner: _Owner


class _Head(BaseModel):
    sha: str


class _User(BaseModel):
    login: str


class _PullRequest(BaseModel):
    number: int
    title: str
    draft: bool = False
    head: _Head
    user: _User


class PullRequestPayload(BaseModel):
    action: str
    installation: _Installation
    repository: _Repository
    pull_request: _PullRequest
