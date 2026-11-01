"""Groq chat completions over httpx, with structured (JSON schema) output.

Rate limiting is per process: Groq reports the tokens left in this minute on every response,
and we wait for the reset before sending a call that won't fit. In prod the worker runs in
one process, so that is effectively global (see docs/decisions.md).
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.github.http import NonRetryableError

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_ATTEMPTS = 3
CALL_TIMEOUT_SECONDS = 60
MAX_COMPLETION_TOKENS = 2048
# Rough output size of one lane call (findings + "low" reasoning), for the pre-send check.
EXPECTED_OUTPUT_TOKENS = 800


class LLMError(Exception):
    """One call failed for good (bad request, invalid output, out of retries). The caller
    skips that call; the review goes on."""


class DeadlineExceeded(Exception):
    """Finishing the call (or waiting for the rate limit) would pass the review deadline."""


@dataclass
class Completion:
    content: str
    tokens_in: int
    tokens_out: int


def parse_duration(value: str) -> float:
    """Groq's reset headers look like "7.66s", "2m59.56s" or "120ms". Returns seconds."""
    units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * units[unit] for n, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value))


class GroqClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._http = http
        self._api_key = api_key
        self._clock = clock
        self._sleep = sleep
        # model -> (tokens left this minute, clock time the minute resets)
        self._token_limits: dict[str, tuple[int, float]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete_json(
        self, model: str, messages: list[dict], schema_name: str, schema: dict, deadline: float
    ) -> Completion:
        """One chat completion whose content is JSON matching `schema` (strict mode).

        Raises LLMError, DeadlineExceeded, or NonRetryableError for a rejected API key.
        """
        body = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "reasoning_effort": "low",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        prompt_chars = sum(len(m["content"]) for m in messages)
        needed_tokens = prompt_chars // 4 + EXPECTED_OUTPUT_TOKENS

        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._wait_for_tokens(model, needed_tokens, deadline)
            timeout = min(CALL_TIMEOUT_SECONDS, deadline - self._clock())
            if timeout <= 0:
                raise DeadlineExceeded("no time left for the call")
            try:
                response = await self._http.post(
                    GROQ_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=timeout,
                )
            except httpx.TransportError as exc:  # timeouts, connection errors
                await self._backoff(attempt, deadline, f"Groq request failed: {exc!r}")
                continue

            self._record_token_limit(model, response)
            if response.is_success:
                data = response.json()
                return Completion(
                    content=data["choices"][0]["message"]["content"],
                    tokens_in=data["usage"]["prompt_tokens"],
                    tokens_out=data["usage"]["completion_tokens"],
                )
            message = f"Groq {response.status_code}: {response.text[:300]}"
            if response.status_code == 429:
                wait = float(response.headers.get("retry-after", 2**attempt))
                await self._wait(wait, deadline)
            elif response.status_code >= 500:
                await self._backoff(attempt, deadline, message)
            elif response.status_code in (401, 403):
                raise NonRetryableError(message)  # bad API key: every call would fail
            else:
                raise LLMError(message)  # e.g. 400 json_validate_failed
        raise LLMError(f"Groq call failed after {MAX_ATTEMPTS} attempts")

    async def _wait_for_tokens(self, model: str, needed: int, deadline: float) -> None:
        remaining, reset_at = self._token_limits.get(model, (None, 0.0))
        now = self._clock()
        if remaining is not None and remaining < needed and reset_at > now:
            await self._wait(reset_at - now, deadline)

    async def _backoff(self, attempt: int, deadline: float, message: str) -> None:
        if attempt == MAX_ATTEMPTS:
            raise LLMError(message)
        await self._wait(2**attempt, deadline)

    async def _wait(self, seconds: float, deadline: float) -> None:
        if self._clock() + seconds >= deadline:
            raise DeadlineExceeded(f"waiting {seconds:.1f}s would pass the review deadline")
        await self._sleep(seconds)

    def _record_token_limit(self, model: str, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        reset = response.headers.get("x-ratelimit-reset-tokens")
        if remaining is not None and reset is not None:
            self._token_limits[model] = (int(remaining), self._clock() + parse_duration(reset))
