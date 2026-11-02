"""arq worker for the load test: the prod job and settings, with GitHub and Groq aimed at the
fakes. Start it with: arq loadtest.worker.LoadTestWorkerSettings"""

import os
import socket

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import get_settings
from app.github.auth import GitHubAppAuth
from app.github.client import GitHubClient
from app.github.http import GITHUB_API_URL
from app.llm.groq import GroqClient
from app.worker import WorkerSettings
from app.worker import startup as app_startup

# The hosts the prod clients call. Requests to them go to the fakes instead.
REWRITTEN_HOSTS = {"api.github.com", "api.groq.com"}


class FakesTransport(httpx.AsyncBaseTransport):
    """Sends GitHub and Groq requests to the fakes, and refuses any other host, so a load test
    can never reach the real services (or spend the real Groq quota)."""

    def __init__(self, fakes_url: str, inner: httpx.AsyncBaseTransport | None = None):
        self._fakes = httpx.URL(fakes_url)
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host not in REWRITTEN_HOSTS and host != self._fakes.host:
            raise RuntimeError(f"the load test doesn't call {host}")
        request.url = request.url.copy_with(
            scheme=self._fakes.scheme, host=self._fakes.host, port=self._fakes.port
        )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def throwaway_private_key() -> str:
    """The fakes don't check the App's JWT, but GitHubAppAuth still signs one."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def fake_clients(
    fakes_url: str, worker: str, inner: httpx.AsyncBaseTransport | None = None
) -> tuple[GitHubClient, GroqClient]:
    """The prod GitHub and Groq clients, sending to the fakes. Each request names the worker,
    so the fakes can tell which container holds which job. Tests pass `inner` to reach the
    fakes in-process."""
    headers = {"X-Loadtest-Worker": worker}
    # Same timeout and redirect handling as make_http_client().
    github_http = httpx.AsyncClient(
        base_url=GITHUB_API_URL,
        timeout=10.0,
        follow_redirects=True,
        headers=headers,
        transport=FakesTransport(fakes_url, inner),
    )
    auth = GitHubAppAuth(github_http, get_settings().github_app_id, throwaway_private_key())
    groq_http = httpx.AsyncClient(headers=headers, transport=FakesTransport(fakes_url, inner))
    return GitHubClient(github_http, auth), GroqClient(groq_http, "fake-groq-key")


async def startup(ctx: dict) -> None:
    await app_startup(ctx)  # logging, the database, and the prod clients...
    await ctx["github"].aclose()  # ...which are swapped for clients aimed at the fakes
    await ctx["groq"].aclose()
    ctx["github"], ctx["groq"] = fake_clients(
        os.environ["LOADTEST_FAKES_URL"], socket.gethostname()
    )


# arq reads only the settings class's own __dict__, so a subclass of WorkerSettings would lose
# its functions, cron jobs and limits. Copy them, and replace only the startup.
LoadTestWorkerSettings = type(
    "LoadTestWorkerSettings",
    (),
    {
        **{k: v for k, v in vars(WorkerSettings).items() if not k.startswith("__")},
        "on_startup": startup,
    },
)
