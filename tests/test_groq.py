import json

import httpx
import pytest
import respx

from app.github.http import NonRetryableError
from app.llm.groq import (
    GROQ_URL,
    MAX_ATTEMPTS,
    DeadlineExceeded,
    GroqClient,
    LLMError,
    ToolCallFailed,
    estimate_tool_call_tokens,
    parse_duration,
)

MESSAGES = [{"role": "user", "content": "x" * 400}]
SCHEMA = {"type": "object"}


class FakeTime:
    """A clock and a sleep that advances it, so waits take no real time."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
async def groq(fake_time):
    client = GroqClient(httpx.AsyncClient(), "key", clock=fake_time.clock, sleep=fake_time.sleep)
    yield client
    await client.aclose()


@pytest.fixture
def groq_api():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(GROQ_URL)


def ok(content='{"findings": []}', headers=None) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    return httpx.Response(200, json=body, headers=headers or {})


async def call(groq, fake_time, deadline_in=600):
    return await groq.complete_json(
        "openai/gpt-oss-120b", MESSAGES, "correctness_findings", SCHEMA, fake_time.now + deadline_in
    )


async def test_returns_content_and_token_usage(groq, groq_api, fake_time):
    groq_api.mock(return_value=ok('{"findings": [1]}'))

    completion = await call(groq, fake_time)

    assert completion.content == '{"findings": [1]}'
    assert (completion.tokens_in, completion.tokens_out) == (120, 30)
    sent = groq_api.calls.last.request
    assert sent.headers["Authorization"] == "Bearer key"
    body = sent.read().decode()
    assert '"strict":true' in body and '"type":"json_schema"' in body


async def test_429_waits_retry_after_then_retries(groq, groq_api, fake_time):
    groq_api.mock(side_effect=[httpx.Response(429, headers={"retry-after": "7"}), ok()])

    await call(groq, fake_time)

    assert fake_time.slept == [7.0]
    assert groq_api.call_count == 2


async def test_429_past_the_deadline_gives_up_without_waiting(groq, groq_api, fake_time):
    # e.g. the daily token quota is used up: Groq says come back in an hour.
    groq_api.mock(return_value=httpx.Response(429, headers={"retry-after": "3600"}))

    with pytest.raises(DeadlineExceeded):
        await call(groq, fake_time, deadline_in=600)

    assert fake_time.slept == []


async def test_server_errors_are_retried_with_backoff(groq, groq_api, fake_time):
    groq_api.mock(side_effect=[httpx.Response(503), httpx.Response(502), ok()])

    await call(groq, fake_time)

    assert fake_time.slept == [2, 4]


async def test_server_errors_give_up_after_max_attempts(groq, groq_api, fake_time):
    groq_api.mock(return_value=httpx.Response(500))

    with pytest.raises(LLMError):
        await call(groq, fake_time)

    assert groq_api.call_count == MAX_ATTEMPTS


async def test_timeouts_are_retried(groq, groq_api, fake_time):
    groq_api.mock(side_effect=[httpx.ReadTimeout("slow"), ok()])

    await call(groq, fake_time)

    assert groq_api.call_count == 2


async def test_bad_request_is_not_retried(groq, groq_api, fake_time):
    groq_api.mock(
        return_value=httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
    )

    with pytest.raises(LLMError):
        await call(groq, fake_time)

    assert groq_api.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_api_key_stops_the_review(groq, groq_api, fake_time, status):
    groq_api.mock(return_value=httpx.Response(status))

    with pytest.raises(NonRetryableError):
        await call(groq, fake_time)


async def test_waits_for_the_token_limit_to_reset_before_sending(groq, groq_api, fake_time):
    # The first response says only 50 tokens are left this minute, resetting in 12 s.
    low = {"x-ratelimit-remaining-tokens": "50", "x-ratelimit-reset-tokens": "12s"}
    groq_api.mock(side_effect=[ok(headers=low), ok()])

    await call(groq, fake_time)
    await call(groq, fake_time)

    assert fake_time.slept == [12.0]


async def test_token_limits_are_tracked_per_model(groq, groq_api, fake_time):
    low = {"x-ratelimit-remaining-tokens": "50", "x-ratelimit-reset-tokens": "12s"}
    groq_api.mock(side_effect=[ok(headers=low), ok()])

    await call(groq, fake_time)
    await groq.complete_json("openai/gpt-oss-20b", MESSAGES, "x", SCHEMA, fake_time.now + 600)

    assert fake_time.slept == []


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("7.66s", 7.66), ("2m59.5s", 179.5), ("120ms", 0.12), ("1h2m3s", 3723)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == pytest.approx(seconds)


TOOLS = [{"type": "function", "function": {"name": "search_repo", "parameters": {}}}]


def tool_reply() -> httpx.Response:
    call = {"id": "c1", "type": "function", "function": {"name": "search_repo", "arguments": "{}"}}
    body = {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 40},
    }
    return httpx.Response(200, json=body)


async def call_tools(groq, fake_time, tool_choice="required"):
    return await groq.complete_tools(
        "openai/gpt-oss-120b", MESSAGES, TOOLS, tool_choice, fake_time.now + 600
    )


async def test_tool_calls_send_tools_and_return_the_assistant_message(groq, groq_api, fake_time):
    groq_api.mock(return_value=tool_reply())

    completion = await call_tools(groq, fake_time)

    assert completion.message["tool_calls"][0]["function"]["name"] == "search_repo"
    assert (completion.tokens_in, completion.tokens_out) == (300, 40)
    body = json.loads(groq_api.calls.last.request.read())
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "required"
    assert body["parallel_tool_calls"] is False
    assert "response_format" not in body  # structured outputs can't be combined with tools


async def test_a_malformed_tool_call_raises_tool_call_failed(groq, groq_api, fake_time):
    error = {"error": {"code": "tool_use_failed", "message": "Failed to call a function"}}
    groq_api.mock(return_value=httpx.Response(400, json=error))

    with pytest.raises(ToolCallFailed):
        await call_tools(groq, fake_time)

    assert groq_api.call_count == 1


async def test_tool_calls_share_the_retry_handling(groq, groq_api, fake_time):
    groq_api.mock(side_effect=[httpx.Response(503), tool_reply()])

    await call_tools(groq, fake_time)

    assert fake_time.slept == [2]


def test_the_tool_call_estimate_counts_the_tool_schemas():
    without_tools = estimate_tool_call_tokens(MESSAGES, [])

    assert estimate_tool_call_tokens(MESSAGES, TOOLS * 10) > without_tools


async def test_tool_calls_report_time_spent_waiting(groq, groq_api, fake_time):
    low = {"x-ratelimit-remaining-tokens": "50", "x-ratelimit-reset-tokens": "12s"}
    first = tool_reply()
    first.headers.update(low)
    groq_api.mock(
        side_effect=[first, httpx.Response(429, headers={"retry-after": "7"}), tool_reply()]
    )

    assert (await call_tools(groq, fake_time)).waited_seconds == 0
    # Waits 12 s for the per-minute reset, then 7 s after a 429.
    assert (await call_tools(groq, fake_time)).waited_seconds == 19
