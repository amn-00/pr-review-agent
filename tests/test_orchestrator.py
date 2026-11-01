import asyncio
import json
from decimal import Decimal

import pytest

from app.db.models import BudgetPurpose
from app.db.queries import add_daily_tokens, daily_tokens_used
from app.github.http import NonRetryableError
from app.llm.groq import Completion, DeadlineExceeded, LLMError
from app.pipeline import orchestrator
from app.pipeline.chunking import Chunk
from app.pipeline.lanes import LANES, estimate_call_tokens
from app.pipeline.orchestrator import (
    MAX_PARALLEL_CALLS,
    PIPELINE_DAILY_TOKEN_BUDGET,
    SKIP_DAILY_BUDGET,
    SKIP_PR_BUDGET,
    SKIP_TIME,
    OrchestratorResult,
    nothing_reviewed_for_daily_budget,
    run_lanes,
    utc_today,
)
from tests.factories import lane_finding, make_chunk

MAIN, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
PIPELINE = BudgetPurpose.PIPELINE
MODELS = {"correctness": MAIN, "security": MAIN, "maintainability": SMALL}
REPO_ID = 1001


class ScriptedGroq:
    """Each call takes `seconds_per_call` of fake time; `fail` decides per file what goes wrong.
    The correctness lane reports one finding on the chunk's last line."""

    def __init__(self, clock, seconds_per_call=10, fail=None, first_round=None):
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.fail = fail or {}
        self.calls: list[tuple[str, str, str]] = []  # (file, lane, model)
        # For deadline tests: calls wait here until this many have started, so the first round
        # of parallel calls passes the deadline check before any of them advances the clock,
        # however long the cache and budget queries before each call happen to take.
        self.first_round = asyncio.Barrier(first_round) if first_round else None

    async def complete_json(self, model, messages, schema_name, schema, deadline) -> Completion:
        lines = messages[1]["content"].splitlines()
        path = next(line.removeprefix("File: ") for line in lines if line.startswith("File: "))
        last_line = int(lines[-2].split()[0])  # the last numbered line before the END marker
        self.calls.append((path, schema_name.removesuffix("_findings"), model))
        if self.first_round:
            await self.first_round.wait()
        else:
            await asyncio.sleep(0)  # let the other workers start their calls, as real I/O would
        self.clock.now += self.seconds_per_call
        if path in self.fail:
            raise self.fail[path]
        if schema_name == "correctness_findings":
            findings = [lane_finding(line_start=last_line, line_end=last_line)]
        else:
            findings = []
        return Completion(json.dumps({"findings": findings}), tokens_in=100, tokens_out=10)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def chunk(path: str, content: str = "x = 1\ny = 2\n", added: set[int] | None = None) -> Chunk:
    return make_chunk(path, content, added)


async def run(groq, sessionmaker, chunks, deadline=1e9, models=MODELS, repo_id=REPO_ID, run_id=1):
    return await run_lanes(
        groq, sessionmaker, models, repo_id, run_id, chunks, deadline, groq.clock
    )


async def test_all_lanes_run_on_every_chunk_and_usage_adds_up(sessionmaker):
    groq = ScriptedGroq(Clock())

    result = await run(groq, sessionmaker, [chunk("a.py"), chunk("b.py")])

    assert len(groq.calls) == 2 * len(LANES)
    assert len(result.candidates) == 2  # one correctness finding per chunk
    usage = result.usage
    assert (usage.tokens_in, usage.tokens_out) == (600, 60)
    assert (usage.llm_calls, usage.cached_calls, usage.tokens_saved) == (6, 0, 0)
    # 4 calls on 120b at $0.15/$0.60 and 2 on 20b at $0.075/$0.30, per million tokens.
    expected = (
        4 * (100 * Decimal("0.15") + 10 * Decimal("0.60"))
        + 2 * (100 * Decimal("0.075") + 10 * Decimal("0.30"))
    ) / 1_000_000
    assert usage.cost_usd == expected


async def test_maintainability_runs_on_the_small_model(sessionmaker):
    groq = ScriptedGroq(Clock())

    await run(groq, sessionmaker, [chunk("a.py")])

    assert {lane: model for _, lane, model in groq.calls} == MODELS


async def test_deadline_skips_docs_and_tests_before_source(sessionmaker):
    groq = ScriptedGroq(Clock(), seconds_per_call=10, first_round=MAX_PARALLEL_CALLS)
    chunks = [chunk("README.md"), chunk("tests/test_app.py"), chunk("app/main.py")]

    # Each call takes 10 s and the deadline is at 5 s, so only the first round (3 calls in
    # parallel) starts in time. It must be the source file's 3 lanes.
    result = await run(groq, sessionmaker, chunks, deadline=5)

    assert [path for path, _, _ in groq.calls] == ["app/main.py"] * len(LANES)
    assert {(c.path, reason) for _, c, reason in result.skipped_calls} == {
        ("tests/test_app.py", SKIP_TIME),
        ("README.md", SKIP_TIME),
    }


async def test_within_a_group_bigger_changes_go_first(sessionmaker):
    groq = ScriptedGroq(Clock(), seconds_per_call=10, first_round=MAX_PARALLEL_CALLS)
    small_change = chunk("app/a.py", "a\nb\nc\n", added={3})
    big_change = chunk("app/b.py", "a\nb\nc\n", added={1, 2, 3})

    await run(groq, sessionmaker, [small_change, big_change], deadline=5)

    assert {path for path, _, _ in groq.calls} == {"app/b.py"}


async def test_a_failed_call_is_counted_and_the_rest_continue(sessionmaker):
    groq = ScriptedGroq(Clock(), fail={"a.py": LLMError("bad output")})

    result = await run(groq, sessionmaker, [chunk("a.py"), chunk("b.py")])

    assert result.failed_calls == len(LANES)
    assert [c.file for c in result.candidates] == ["b.py"]


async def test_deadline_hit_inside_a_call_counts_as_skipped(sessionmaker):
    groq = ScriptedGroq(Clock(), fail={"a.py": DeadlineExceeded("rate limit wait too long")})

    result = await run(groq, sessionmaker, [chunk("a.py")])

    assert [reason for _, _, reason in result.skipped_calls] == [SKIP_TIME] * len(LANES)
    assert result.failed_calls == 0


async def test_rejected_api_key_stops_the_whole_review(sessionmaker):
    groq = ScriptedGroq(Clock(), fail={"a.py": NonRetryableError("Groq 401")})

    with pytest.raises(NonRetryableError):
        await run(groq, sessionmaker, [chunk("a.py"), chunk("b.py")])


async def test_unchanged_chunks_come_from_the_cache(sessionmaker):
    first = ScriptedGroq(Clock())
    await run(first, sessionmaker, [chunk("a.py")], run_id=1)
    second = ScriptedGroq(Clock())

    # A later run (e.g. the next push) on the same code.
    result = await run(second, sessionmaker, [chunk("a.py")], run_id=2)

    assert second.calls == []
    usage = result.usage
    assert (usage.llm_calls, usage.cached_calls) == (0, len(LANES))
    assert usage.tokens_saved == len(LANES) * 110  # what the first run paid
    assert (usage.tokens_in, usage.tokens_out, usage.cost_usd) == (0, 0, 0)
    [candidate] = result.candidates
    assert (candidate.line_start, candidate.line_end) == (2, 2)


async def test_reusing_the_same_runs_calls_is_not_counted_as_saved(sessionmaker):
    # A retry of run 1 reuses what run 1's first attempt paid for: nothing saved overall.
    await run(ScriptedGroq(Clock()), sessionmaker, [chunk("a.py")], run_id=1)

    result = await run(ScriptedGroq(Clock()), sessionmaker, [chunk("a.py")], run_id=1)

    assert result.usage.cached_calls == len(LANES)
    assert result.usage.tokens_saved == 0


async def test_a_moved_chunk_hits_the_cache_at_its_new_lines(sessionmaker):
    original = make_chunk("a.py", "x = 1\ny = 2\n")
    await run(ScriptedGroq(Clock()), sessionmaker, [original])
    # Same code, now 40 lines further down (lines were added above it).
    moved = Chunk(
        **{
            **vars(original),
            "start": 41,
            "end": 42,
            "text": original.text.replace("    1", "   41").replace("    2", "   42"),
        }
    )
    second = ScriptedGroq(Clock())

    result = await run(second, sessionmaker, [moved])

    assert second.calls == []
    [candidate] = result.candidates
    assert (candidate.line_start, candidate.line_end) == (42, 42)


async def test_cache_is_not_shared_across_repos_or_models(sessionmaker):
    await run(ScriptedGroq(Clock()), sessionmaker, [chunk("a.py")])

    other_repo = ScriptedGroq(Clock())
    await run(other_repo, sessionmaker, [chunk("a.py")], repo_id=REPO_ID + 1)
    other_model = ScriptedGroq(Clock())
    await run(other_model, sessionmaker, [chunk("a.py")], models={lane: "m2" for lane in LANES})

    assert len(other_repo.calls) == len(LANES)
    assert len(other_model.calls) == len(LANES)


async def test_failed_calls_are_not_cached(sessionmaker):
    await run(ScriptedGroq(Clock(), fail={"a.py": LLMError("x")}), sessionmaker, [chunk("a.py")])
    second = ScriptedGroq(Clock())

    await run(second, sessionmaker, [chunk("a.py")])

    assert len(second.calls) == len(LANES)


async def test_pr_budget_stops_new_calls(sessionmaker, monkeypatch):
    c = chunk("a.py")
    # Room for exactly the first call's estimate: after it spends 110 tokens, nothing fits.
    monkeypatch.setattr(orchestrator, "PR_TOKEN_BUDGET", estimate_call_tokens("correctness", c) + 1)
    monkeypatch.setattr(orchestrator, "MAX_PARALLEL_CALLS", 1)
    groq = ScriptedGroq(Clock())

    result = await run(groq, sessionmaker, [c])

    assert len(groq.calls) == 1
    assert [reason for _, _, reason in result.skipped_calls] == [SKIP_PR_BUDGET] * 2


async def test_daily_budget_is_per_model(sessionmaker):
    # The main model has used up today's budget; the small model hasn't.
    async with sessionmaker() as session:
        await add_daily_tokens(
            session, MAIN, utc_today(), BudgetPurpose.PIPELINE, PIPELINE_DAILY_TOKEN_BUDGET
        )
    groq = ScriptedGroq(Clock())

    result = await run(groq, sessionmaker, [chunk("a.py")])

    assert [(lane, model) for _, lane, model in groq.calls] == [("maintainability", SMALL)]
    assert sorted(
        lane for lane, _, reason in result.skipped_calls if reason == SKIP_DAILY_BUDGET
    ) == [
        "correctness",
        "security",
    ]


async def test_spent_tokens_are_added_to_todays_usage(sessionmaker):
    await run(ScriptedGroq(Clock()), sessionmaker, [chunk("a.py")])

    async with sessionmaker() as session:
        assert await daily_tokens_used(session, MAIN, utc_today(), PIPELINE) == 2 * 110
        assert await daily_tokens_used(session, SMALL, utc_today(), PIPELINE) == 110
        assert await daily_tokens_used(session, MAIN, utc_today(), BudgetPurpose.AGENT) == 0


async def test_the_agents_spending_doesnt_use_the_pipelines_budget(sessionmaker):
    async with sessionmaker() as session:
        await add_daily_tokens(session, MAIN, utc_today(), BudgetPurpose.AGENT, 10**6)
    groq = ScriptedGroq(Clock())

    result = await run(groq, sessionmaker, [chunk("a.py")])

    assert len(groq.calls) == len(LANES)
    assert result.skipped_calls == []


def test_nothing_reviewed_for_daily_budget():
    c = chunk("a.py")
    blocked = OrchestratorResult(skipped_calls=[("correctness", c, SKIP_DAILY_BUDGET)])
    partly_cached = OrchestratorResult(skipped_calls=[("correctness", c, SKIP_DAILY_BUDGET)])
    partly_cached.usage.cached_calls = 1
    out_of_time = OrchestratorResult(skipped_calls=[("correctness", c, SKIP_TIME)])

    assert nothing_reviewed_for_daily_budget(blocked) is True
    assert nothing_reviewed_for_daily_budget(partly_cached) is False
    assert nothing_reviewed_for_daily_budget(out_of_time) is False
