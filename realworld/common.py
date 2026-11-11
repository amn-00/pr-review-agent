"""Shared by the real-world eval scripts: GitHub as the owner, and the prod database read-only."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# The only account these scripts may push to or open pull requests in.
OWNER = "amn-00"
GITHUB_API = "https://api.github.com"
GET_TRIES = 3
GET_RETRY_SECONDS = 2


def gh_token() -> str:
    """The owner's GitHub token, from the GitHub CLI's login. It stays in memory: it's never
    put in an env var, a file or a log."""
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        sys.exit("The GitHub CLI isn't installed or isn't logged in. Run: gh auth login")
    return result.stdout.strip()


def github_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=GITHUB_API,
        timeout=30.0,
        follow_redirects=True,  # GitHub redirects requests for renamed repos
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-review-agent-realworld-eval",
        },
    )


def get(http: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """GET, trying again after a timeout or a dropped connection. The selection makes a few
    thousand requests in a row, and one slow answer shouldn't end the run (it did once)."""
    for attempt in range(1, GET_TRIES + 1):
        try:
            response = http.get(url, **kwargs)
            break
        except httpx.TransportError:
            if attempt == GET_TRIES:
                raise
            time.sleep(GET_RETRY_SECONDS * attempt)
    response.raise_for_status()
    return response


def iter_items(http: httpx.Client, url: str, params: dict | None = None) -> Iterator[dict]:
    """Every item of a paginated list endpoint, following the Link header. It's a generator, so
    a caller can stop early and the remaining pages are never requested."""
    next_url: str | None = url
    page_params: dict | None = {"per_page": 100, **(params or {})}
    while next_url:
        response = get(http, next_url, params=page_params)
        yield from response.json()
        next_url = response.links.get("next", {}).get("url")
        page_params = None  # the "next" URL already carries the query string


def prod_database_url() -> str:
    """The Neon URL, from PROD_DATABASE_URL only: never from .env, which is the local stack's."""
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        sys.exit(
            "Set PROD_DATABASE_URL to the rewritten Neon URL (docs/deploy.md, step 1), and "
            "PGSSLROOTCERT to a CA bundle."
        )
    return url


def read_only_engine(url: str) -> AsyncEngine:
    """An engine whose every transaction is read-only. These scripts only read the prod
    database, and with this a bug in them can't write to it."""
    return create_async_engine(
        url, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )
