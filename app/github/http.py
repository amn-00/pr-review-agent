"""Shared HTTP plumbing for GitHub calls: one client config, one error classification."""

import time

import httpx

from app.config import Settings


class RetryableError(Exception):
    """Transient failure (timeout, 5xx, rate limit). The job should retry later."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        # Seconds GitHub told us to wait, if it said so.
        self.retry_after = retry_after


class NonRetryableError(Exception):
    """Permanent failure (bad credentials, missing permission, 404). Retrying won't help."""


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.github_api_url,
        timeout=10.0,
        follow_redirects=True,  # GitHub redirects requests for renamed repos
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review-agent",
        },
    )


async def send(http: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Make a request, turning timeouts and connection errors into RetryableError.

    Does not check the status code: callers call raise_for_status() themselves, after reading
    any headers they need (rate limits are reported on error responses too).
    """
    try:
        return await http.request(method, url, **kwargs)
    except httpx.TransportError as exc:  # timeouts, connection errors
        raise RetryableError(f"GitHub {method} {url} failed: {exc!r}") from exc


def raise_for_status(response: httpx.Response) -> None:
    """Classify an error response as RetryableError or NonRetryableError."""
    if response.is_success:
        return
    status = response.status_code
    message = f"GitHub {response.request.method} {response.request.url.path} -> {status}: "
    message += response.text[:200]
    retry_after = _retry_after_seconds(response)
    # A 403 is a rate limit only if GitHub says when to retry; otherwise it's a permission error.
    if status >= 500 or status == 429 or (status == 403 and retry_after is not None):
        raise RetryableError(message, retry_after=retry_after)
    raise NonRetryableError(message)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    headers = response.headers
    if "retry-after" in headers:  # secondary rate limit
        return float(headers["retry-after"])
    if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
        return max(0.0, float(headers["x-ratelimit-reset"]) - time.time())  # primary rate limit
    return None
