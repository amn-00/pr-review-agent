import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.auth import GitHubAppAuth
from app.github.http import NonRetryableError, RetryableError
from tests.conftest import GITHUB_API

TOKEN_URL = "/app/installations/555/access_tokens"
START = 1_800_000_000.0  # a fixed "now" for the fake clock


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_key_pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def http():
    async with httpx.AsyncClient(base_url=GITHUB_API) as client:
        yield client


def token_response(token: str, expires_at: float) -> httpx.Response:
    iso = datetime.fromtimestamp(expires_at, UTC).isoformat().replace("+00:00", "Z")
    return httpx.Response(201, json={"token": token, "expires_at": iso})


def test_jwt_is_signed_by_the_app_and_short_lived(http, private_key, private_key_pem):
    # Real time here, so PyJWT can check iat/exp like GitHub does.
    now = time.time()
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=FakeClock(now))

    token = auth.make_jwt()
    claims = jwt.decode(token, private_key.public_key(), algorithms=["RS256"])

    assert claims["iss"] == "12345"
    assert claims["iat"] == int(now) - 60
    assert claims["exp"] - claims["iat"] <= 600  # GitHub rejects JWTs valid for > 10 min


async def test_exchanges_jwt_for_installation_token(http, private_key_pem, github_api):
    route = github_api.post(TOKEN_URL).mock(return_value=token_response("tok-1", START + 3600))
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=FakeClock(START))

    assert await auth.installation_token(555) == "tok-1"
    assert route.calls.last.request.headers["Authorization"].startswith("Bearer ey")


async def test_token_is_cached_until_close_to_expiry(http, private_key_pem, github_api):
    route = github_api.post(TOKEN_URL).mock(
        side_effect=[
            token_response("tok-1", START + 3600),
            token_response("tok-2", START + 7200),
        ]
    )
    clock = FakeClock(START)
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=clock)

    assert await auth.installation_token(555) == "tok-1"
    clock.now = START + 3000  # 10 min before expiry: still cached
    assert await auth.installation_token(555) == "tok-1"
    assert route.call_count == 1

    clock.now = START + 3600 - 4 * 60  # inside the 5-minute refresh margin
    assert await auth.installation_token(555) == "tok-2"
    assert route.call_count == 2


async def test_tokens_are_cached_per_installation(http, private_key_pem, github_api):
    github_api.post(TOKEN_URL).mock(return_value=token_response("tok-a", START + 3600))
    github_api.post("/app/installations/777/access_tokens").mock(
        return_value=token_response("tok-b", START + 3600)
    )
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=FakeClock(START))

    assert await auth.installation_token(555) == "tok-a"
    assert await auth.installation_token(777) == "tok-b"


async def test_rejected_credentials_are_not_retried(http, private_key_pem, github_api):
    github_api.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"message": "Bad"}))
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=FakeClock(START))

    with pytest.raises(NonRetryableError):
        await auth.installation_token(555)


async def test_github_outage_during_exchange_is_retryable(http, private_key_pem, github_api):
    github_api.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    auth = GitHubAppAuth(http, "12345", private_key_pem, clock=FakeClock(START))

    with pytest.raises(RetryableError):
        await auth.installation_token(555)
