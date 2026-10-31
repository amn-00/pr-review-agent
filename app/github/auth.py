import time
from collections.abc import Callable
from datetime import datetime

import httpx
import jwt

from app.github.http import raise_for_status, send

# Refresh installation tokens this long before GitHub expires them (they live 1 hour).
TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60


class GitHubAppAuth:
    """Authenticates as the GitHub App, then as each installation of it.

    1. Sign a short-lived JWT with the App's private key (proves we are the App).
    2. Exchange it for an installation access token (lets us act on that installation's repos).

    Tokens are cached in memory per process. If two coroutines miss the cache at once, both
    fetch a token; that wastes one request but is harmless.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        app_id: str,
        private_key_pem: str,
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._app_id = app_id
        self._private_key = private_key_pem
        self._clock = clock
        self._tokens: dict[int, tuple[str, float]] = {}  # installation id -> (token, expires_at)

    def make_jwt(self) -> str:
        now = int(self._clock())
        claims = {
            "iat": now - 60,  # backdated in case our clock is ahead of GitHub's
            "exp": now + 9 * 60,  # GitHub allows at most 10 minutes
            "iss": self._app_id,
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > self._clock():
            return cached[0]

        response = await send(
            self._http,
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.make_jwt()}"},
        )
        raise_for_status(response)
        data = response.json()
        expires_at = datetime.fromisoformat(data["expires_at"]).timestamp()
        self._tokens[installation_id] = (data["token"], expires_at)
        return data["token"]
