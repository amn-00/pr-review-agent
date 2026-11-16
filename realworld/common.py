"""Shared by the real-world eval scripts: GitHub as the owner, and the prod database read-only."""

import csv
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# The only account these scripts may push to or open pull requests in.
OWNER = "amn-00"
GITHUB_API = "https://api.github.com"
GET_TRIES = 3
GET_RETRY_SECONDS = 2
# Cloudflare's DNS-over-HTTPS endpoint with JSON answers.
DNS_OVER_HTTPS = "https://cloudflare-dns.com/dns-query"


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


def use_public_dns_if_needed(host: str) -> None:
    """If this machine's DNS can't resolve `host`, look its address up over HTTPS and use that
    for the rest of this process.

    The owner's home router refuses to resolve Neon's endpoint names, while public DNS servers
    answer. Only the address lookup changes: TLS still gets the real name, so the certificate
    and hostname are verified as before (`ssl=verify-full`).
    """
    try:
        socket.getaddrinfo(host, None)
        return
    except socket.gaierror:
        pass
    response = httpx.get(
        DNS_OVER_HTTPS,
        params={"name": host, "type": "A"},
        headers={"Accept": "application/dns-json"},
        timeout=15.0,
    )
    response.raise_for_status()
    # Type 1 is an A record; the answer can also hold the CNAMEs on the way to it.
    addresses = [a["data"] for a in response.json().get("Answer", []) if a["type"] == 1]
    if not addresses:
        sys.exit(f"No address found for {host}, by this machine's DNS or over HTTPS.")
    system_lookup = socket.getaddrinfo

    def lookup(name, *args, **kwargs):
        return system_lookup(addresses[0] if name == host else name, *args, **kwargs)

    socket.getaddrinfo = lookup


def read_only_engine(url: str) -> AsyncEngine:
    """An engine whose every transaction is read-only. These scripts only read the prod
    database, and with this a bug in them can't write to it."""
    use_public_dns_if_needed(make_url(url).host)
    return create_async_engine(
        url, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )


def read_rows(path: Path) -> list[dict]:
    """The rows of one of the hand-filled sheets; none if it doesn't exist yet."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def merge_rows(path: Path, columns: list[str], new_rows: list[dict]) -> list[dict]:
    """Write a sheet with a row for every `comment_id` already in it or in `new_rows`. A row
    that's already there is kept as it is, so what was filled in by hand survives a re-run.
    Ordered by PR, file and line. Returns the rows written."""
    rows = {row["comment_id"]: row for row in read_rows(path)}
    for row in new_rows:
        rows.setdefault(str(row["comment_id"]), row)
    ordered = sorted(rows.values(), key=lambda row: (int(row["k"]), row["file"], int(row["line"])))
    # utf-8-sig: Excel reads the file as UTF-8 only with this marker.
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, columns)
        writer.writeheader()
        writer.writerows(ordered)
    return ordered
