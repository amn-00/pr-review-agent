import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from urllib.parse import quote

import httpx

from app.config import Settings
from app.github.auth import GitHubAppAuth
from app.github.http import RetryableError, make_http_client, raise_for_status, send

# Stop calling GitHub (and defer the job) when fewer requests than this remain.
MIN_RATE_LIMIT_REMAINING = 50


class GitHubClient:
    """The GitHub REST calls the worker needs.

    One instance per process. Methods take the installation ID because each installation
    has its own token and its own rate limit.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: GitHubAppAuth,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._auth = auth
        self._clock = clock
        # installation id -> (requests remaining, unix time the limit resets), from the
        # X-RateLimit-* headers of the last response.
        self._rate_limits: dict[int, tuple[int, float]] = {}
        self._bot_login: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubClient":
        http = make_http_client()
        auth = GitHubAppAuth(http, settings.github_app_id, settings.github_private_key_pem())
        return cls(http, auth)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_pull(self, installation_id: int, repo: str, number: int) -> dict:
        response = await self._request(installation_id, "GET", f"/repos/{repo}/pulls/{number}")
        return response.json()

    async def list_pull_files(self, installation_id: int, repo: str, number: int) -> list[dict]:
        """Changed files with their `patch` (missing for binary or very large diffs)."""
        url = f"/repos/{repo}/pulls/{number}/files"
        return [file async for file in self._iter_items(installation_id, url)]

    async def get_file_content(self, installation_id: int, repo: str, path: str, ref: str) -> str:
        """A file's full text at a commit. The raw media type returns the bytes directly
        instead of base64 JSON, and works for files over 1 MB."""
        response = await self._request(
            installation_id,
            "GET",
            f"/repos/{repo}/contents/{quote(path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        return response.text

    async def list_reviews(self, installation_id: int, repo: str, number: int) -> list[dict]:
        url = f"/repos/{repo}/pulls/{number}/reviews"
        return [review async for review in self._iter_items(installation_id, url)]

    async def create_review(
        self,
        installation_id: int,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict],
    ) -> dict:
        response = await self._request(
            installation_id,
            "POST",
            f"/repos/{repo}/pulls/{number}/reviews",
            json={"commit_id": commit_id, "event": "COMMENT", "body": body, "comments": comments},
        )
        return response.json()

    async def bot_login(self) -> str:
        """The login our App posts as, e.g. "my-app[bot]". Looked up once via GET /app.

        Humans can't register logins containing "[" or "]", so only our App can author as
        this account.
        """
        if self._bot_login is None:
            response = await self._app_request("/app")
            self._bot_login = f"{response.json()['slug']}[bot]"
        return self._bot_login

    async def list_installations(self) -> list[dict]:
        """Every installation of our App (each has `id` and `suspended_at`)."""
        return [item async for item in self._iter_items(None, "/app/installations")]

    async def list_installation_repos(self, installation_id: int) -> list[dict]:
        url = "/installation/repositories"
        items = self._iter_items(installation_id, url, items_key="repositories")
        return [repo async for repo in items]

    async def list_open_pulls(
        self, installation_id: int, repo: str, updated_since: datetime
    ) -> list[dict]:
        """Open PRs updated since `updated_since`, newest first. Stops paging at the first
        older one, so a repo with many old PRs costs one request."""
        params = {"state": "open", "sort": "updated", "direction": "desc"}
        pulls: list[dict] = []
        async for pull in self._iter_items(installation_id, f"/repos/{repo}/pulls", params):
            if datetime.fromisoformat(pull["updated_at"]) < updated_since:
                break
            pulls.append(pull)
        return pulls

    async def _iter_items(
        self,
        installation_id: int | None,
        url: str,
        params: dict | None = None,
        items_key: str | None = None,
    ) -> AsyncIterator[dict]:
        """Yield every item of a paginated list endpoint, following the Link header.

        `installation_id=None` authenticates as the App itself (JWT). `items_key` is for
        endpoints that wrap the list in an object, like {"repositories": [...]}.
        """
        next_url: str | None = url
        page_params: dict | None = {"per_page": 100, **(params or {})}
        while next_url:
            if installation_id is None:
                response = await self._app_request(next_url, params=page_params)
            else:
                response = await self._request(installation_id, "GET", next_url, params=page_params)
            data = response.json()
            for item in data[items_key] if items_key else data:
                yield item
            next_url = response.links.get("next", {}).get("url")
            page_params = None  # the "next" URL already carries the query string

    async def _app_request(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET as the App itself (JWT), for endpoints about the App, not one installation."""
        headers = {"Authorization": f"Bearer {self._auth.make_jwt()}"}
        response = await send(self._http, "GET", url, params=params, headers=headers)
        raise_for_status(response)
        return response

    async def _request(
        self, installation_id: int, method: str, url: str, headers: dict | None = None, **kwargs
    ) -> httpx.Response:
        self._wait_if_rate_limited(installation_id)
        token = await self._auth.installation_token(installation_id)
        headers = {**(headers or {}), "Authorization": f"Bearer {token}"}
        response = await send(self._http, method, url, headers=headers, **kwargs)
        self._record_rate_limit(installation_id, response)
        raise_for_status(response)
        return response

    def _wait_if_rate_limited(self, installation_id: int) -> None:
        """Back off before hitting the limit, instead of burning the last requests and
        getting 403s. Raises RetryableError so the job is deferred until the reset."""
        remaining, reset_at = self._rate_limits.get(installation_id, (None, 0.0))
        now = self._clock()
        if remaining is not None and remaining < MIN_RATE_LIMIT_REMAINING and reset_at > now:
            raise RetryableError(
                f"GitHub rate limit nearly used up ({remaining} left)",
                retry_after=reset_at - now,
            )

    def _record_rate_limit(self, installation_id: int, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None:
            self._rate_limits[installation_id] = (int(remaining), float(reset))
