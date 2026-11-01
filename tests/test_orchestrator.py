import asyncio
import json

import pytest

from app.github.http import NonRetryableError
from app.llm.groq import Completion, DeadlineExceeded, LLMError
from app.pipeline.chunking import Chunk
from app.pipeline.lanes import LANES
from app.pipeline.orchestrator import run_lanes
from tests.factories import lane_finding


class ScriptedGroq:
    """Each call takes `seconds_per_call` of fake time; `fail` decides per file what goes wrong."""

    def __init__(self, clock, seconds_per_call=10, fail=None):
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.fail = fail or {}
        self.called_files: list[str] = []

    async def complete_json(self, model, messages, schema_name, schema, deadline) -> Completion:
        lines = messages[1]["content"].splitlines()
        path = next(line.removeprefix("File: ") for line in lines if line.startswith("File: "))
        self.called_files.append(path)
        await asyncio.sleep(0)  # let the other workers start their calls, as real I/O would
        self.clock.now += self.seconds_per_call
        if path in self.fail:
            raise self.fail[path]
        findings = [lane_finding()] if schema_name == "correctness_findings" else []
        return Completion(json.dumps({"findings": findings}), tokens_in=100, tokens_out=10)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def chunk(path: str) -> Chunk:
    return Chunk(path, 1, 3, "    3 + x = 1")


async def test_all_lanes_run_on_every_chunk_and_tokens_add_up():
    clock = Clock()
    groq = ScriptedGroq(clock)

    result = await run_lanes(groq, "m", [chunk("a.py"), chunk("b.py")], deadline=1e9, clock=clock)

    assert len(groq.called_files) == 2 * len(LANES)
    assert len(result.candidates) == 2  # one correctness finding per chunk
    assert (result.tokens_in, result.tokens_out) == (600, 60)
    assert result.failed_calls == 0 and result.skipped_calls == []


async def test_deadline_skips_docs_and_tests_before_source():
    clock = Clock()
    groq = ScriptedGroq(clock, seconds_per_call=10)
    chunks = [chunk("README.md"), chunk("tests/test_app.py"), chunk("app/main.py")]

    # Each call takes 10 s and the deadline is at 5 s, so only the first round (3 calls in
    # parallel) starts in time. It must be the source file's 3 lanes.
    result = await run_lanes(groq, "m", chunks, deadline=5, clock=clock)

    assert groq.called_files == ["app/main.py"] * len(LANES)
    assert {c.path for _, c in result.skipped_calls} == {"tests/test_app.py", "README.md"}


async def test_a_failed_call_is_counted_and_the_rest_continue():
    clock = Clock()
    groq = ScriptedGroq(clock, fail={"a.py": LLMError("bad output")})

    result = await run_lanes(groq, "m", [chunk("a.py"), chunk("b.py")], deadline=1e9, clock=clock)

    assert result.failed_calls == len(LANES)
    assert [c.file for c in result.candidates] == ["b.py"]


async def test_deadline_hit_inside_a_call_counts_as_skipped():
    clock = Clock()
    groq = ScriptedGroq(clock, fail={"a.py": DeadlineExceeded("rate limit wait too long")})

    result = await run_lanes(groq, "m", [chunk("a.py")], deadline=1e9, clock=clock)

    assert len(result.skipped_calls) == len(LANES)
    assert result.failed_calls == 0


async def test_rejected_api_key_stops_the_whole_review():
    clock = Clock()
    groq = ScriptedGroq(clock, fail={"a.py": NonRetryableError("Groq 401")})

    with pytest.raises(NonRetryableError):
        await run_lanes(groq, "m", [chunk("a.py"), chunk("b.py")], deadline=1e9, clock=clock)
