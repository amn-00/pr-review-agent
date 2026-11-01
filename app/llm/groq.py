"""Groq chat completions over httpx: structured (JSON schema) output for the lanes, and tool
calling for the verification agent.

Rate limiting is per process: Groq reports the tokens left in this minute on every response,
and we wait for the reset before sending a call that won't fit. In prod the worker runs in
one process, so that is effectively global (see docs/decisions.md).
"""

import asyncio
import json
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
# An agent step's output is one tool call plus "low" reasoning.
TOOL_CALL_MAX_COMPLETION_TOKENS = 1024
EXPECTED_TOOL_CALL_OUTPUT_TOKENS = 300


class LLMError(Exception):
    """One call failed for good (bad request, invalid output, out of retries). The caller
    skips that call; the review goes on."""


class ToolCallFailed(LLMError):
    """The model's output for this call was unusable, and asking again usually works:
    - `tool_use_failed`: a tool call that doesn't parse or doesn't match the schema;
    - `output_parse_failed`: output that isn't a tool call at all (e.g. prose; seen in the
      Phase 5 smoke run).

    It's an LLMError, so callers that don't retry treat it as any failed call."""


# Groq error codes that mean "this generation was unusable", not "this request is wrong".
FAILED_GENERATION_CODES = ("tool_use_failed", "output_parse_failed")


class DeadlineExceeded(Exception):
    """Finishing the call (or waiting for the rate limit) would pass the review deadline."""


@dataclass
class Completion:
    content: str
    tokens_in: int
    tokens_out: int
    # Time spent waiting for rate limits or backoff (see ToolCompletion), for eval latency.
    waited_seconds: float = 0.0


@dataclass
class ToolCompletion:
    message: dict  # the assistant message, with its `tool_calls`
    tokens_in: int
    tokens_out: int
    # Time spent waiting before or between attempts (rate limits, backoff), so the agent's
    # per-finding time limit can leave out waits caused by quota rather than by the agent.
    waited_seconds: float = 0.0


def estimate_tool_call_tokens(messages: list[dict], tools: list[dict]) -> int:
    """Tokens one agent step will probably use. The tool schemas and earlier tool calls are
    sent too, so the whole request counts, not only the message texts."""
    request_chars = len(json.dumps(messages)) + len(json.dumps(tools))
    return request_chars // 4 + EXPECTED_TOOL_CALL_OUTPUT_TOKENS


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
        data, waited = await self._post(model, body, needed_tokens, deadline)
        return Completion(
            content=data["choices"][0]["message"]["content"],
            tokens_in=data["usage"]["prompt_tokens"],
            tokens_out=data["usage"]["completion_tokens"],
            waited_seconds=waited,
        )

    async def complete_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str | dict,
        deadline: float,
    ) -> ToolCompletion:
        """One agent step: the model answers with tool calls. `tool_choice` is "required" or
        names one function to force it. Structured outputs can't be combined with tools, so
        the caller validates the arguments itself.

        Raises ToolCallFailed, LLMError, DeadlineExceeded, or NonRetryableError.
        """
        body = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "parallel_tool_calls": False,
            "max_completion_tokens": TOOL_CALL_MAX_COMPLETION_TOKENS,
            "reasoning_effort": "low",
        }
        needed_tokens = estimate_tool_call_tokens(messages, tools)
        data, waited = await self._post(model, body, needed_tokens, deadline)
        return ToolCompletion(
            message=data["choices"][0]["message"],
            tokens_in=data["usage"]["prompt_tokens"],
            tokens_out=data["usage"]["completion_tokens"],
            waited_seconds=waited,
        )

    async def _post(
        self, model: str, body: dict, needed_tokens: int, deadline: float
    ) -> tuple[dict, float]:
        """Send one request, with rate limiting, retries and the deadline. Returns the JSON and
        the seconds spent waiting."""
        waited = 0.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            waited += await self._wait_for_tokens(model, needed_tokens, deadline)
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
                waited += await self._backoff(attempt, deadline, f"Groq request failed: {exc!r}")
                continue

            self._record_token_limit(model, response)
            if response.is_success:
                return response.json(), waited
            message = f"Groq {response.status_code}: {response.text[:300]}"
            if response.status_code == 429:
                wait = float(response.headers.get("retry-after", 2**attempt))
                waited += await self._wait(wait, deadline)
            elif response.status_code >= 500:
                waited += await self._backoff(attempt, deadline, message)
            elif response.status_code in (401, 403):
                raise NonRetryableError(message)  # bad API key: every call would fail
            elif any(code in response.text for code in FAILED_GENERATION_CODES):
                raise ToolCallFailed(message)
            else:
                raise LLMError(message)  # e.g. 400 json_validate_failed
        raise LLMError(f"Groq call failed after {MAX_ATTEMPTS} attempts")

    async def _wait_for_tokens(self, model: str, needed: int, deadline: float) -> float:
        remaining, reset_at = self._token_limits.get(model, (None, 0.0))
        now = self._clock()
        if remaining is not None and remaining < needed and reset_at > now:
            return await self._wait(reset_at - now, deadline)
        return 0.0

    async def _backoff(self, attempt: int, deadline: float, message: str) -> float:
        if attempt == MAX_ATTEMPTS:
            raise LLMError(message)
        return await self._wait(2**attempt, deadline)

    async def _wait(self, seconds: float, deadline: float) -> float:
        if self._clock() + seconds >= deadline:
            raise DeadlineExceeded(f"waiting {seconds:.1f}s would pass the review deadline")
        await self._sleep(seconds)
        return seconds

    def _record_token_limit(self, model: str, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        reset = response.headers.get("x-ratelimit-reset-tokens")
        if remaining is not None and reset is not None:
            self._token_limits[model] = (int(remaining), self._clock() + parse_duration(reset))
