import time
from collections.abc import Callable

import httpx

from app.config import Settings
from app.github.auth import GitHubAppAuth
from app.github.http import RetryableError, make_http_client, raise_for_status, send


class GitHubClient:
    """The GitHub REST calls the worker needs.

    One instance per process. Methods take the installation ID because each installation
    has its own token and its own rate limit.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: GitHubAppAuth,
        min_rate_limit_remaining: int,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._auth = auth
        self._min_remaining = min_rate_limit_remaining
        self._clock = clock
        # installation id -> (requests remaining, unix time the limit resets), from the
        # X-RateLimit-* headers of the last response.
        self._rate_limits: dict[int, tuple[int, float]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubClient":
        http = make_http_client(settings)
        auth = GitHubAppAuth(http, settings.github_app_id, settings.github_private_key_pem())
        return cls(http, auth, settings.github_min_rate_limit_remaining)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_pull(self, installation_id: int, repo: str, number: int) -> dict:
        response = await self._request(installation_id, "GET", f"/repos/{repo}/pulls/{number}")
        return response.json()

    async def list_issue_comments(self, installation_id: int, repo: str, number: int) -> list[dict]:
        """All top-level comments on a PR (PRs are issues in GitHub's API), following pagination."""
        comments: list[dict] = []
        url: str | None = f"/repos/{repo}/issues/{number}/comments"
        params: dict | None = {"per_page": 100}
        while url:
            response = await self._request(installation_id, "GET", url, params=params)
            comments.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the "next" URL already carries the query string
        return comments

    async def create_issue_comment(
        self, installation_id: int, repo: str, number: int, body: str
    ) -> dict:
        response = await self._request(
            installation_id,
            "POST",
            f"/repos/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        return response.json()

    async def _request(
        self, installation_id: int, method: str, url: str, **kwargs
    ) -> httpx.Response:
        self._wait_if_rate_limited(installation_id)
        token = await self._auth.installation_token(installation_id)
        response = await send(
            self._http, method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        self._record_rate_limit(installation_id, response)
        raise_for_status(response)
        return response

    def _wait_if_rate_limited(self, installation_id: int) -> None:
        """Back off before hitting the limit, instead of burning the last requests and
        getting 403s. Raises RetryableError so the job is deferred until the reset."""
        remaining, reset_at = self._rate_limits.get(installation_id, (None, 0.0))
        now = self._clock()
        if remaining is not None and remaining < self._min_remaining and reset_at > now:
            raise RetryableError(
                f"GitHub rate limit nearly used up ({remaining} left)",
                retry_after=reset_at - now,
            )

    def _record_rate_limit(self, installation_id: int, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None:
            self._rate_limits[installation_id] = (int(remaining), float(reset))
