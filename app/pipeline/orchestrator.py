"""Run every lane over every chunk, a few calls at a time, within time and token budgets."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BudgetPurpose
from app.db.queries import (
    add_daily_tokens,
    add_run_usage,
    daily_tokens_used,
    get_cached,
    put_cached,
)
from app.llm.groq import DeadlineExceeded, GroqClient, LLMError
from app.llm.pricing import cost_usd
from app.pipeline.cache import cache_key, from_cached, to_cached
from app.pipeline.chunking import Chunk, priority
from app.pipeline.lanes import LANES, Candidate, estimate_call_tokens, run_lane
from app.pipeline.usage import Usage

logger = logging.getLogger(__name__)

# Calls in flight at once. More doesn't help: Groq's tokens-per-minute limit is the bottleneck.
MAX_PARALLEL_CALLS = 3
# Tokens of real LLM calls one run may spend (cache hits are free), so one big PR can't take
# the whole day: 200K/day leaves room for at least 4 PRs.
PR_TOKEN_BUDGET = 50_000
# The lanes' share of each model's daily tokens, across all PRs. Groq's free limit is 200K:
# 110K for the lanes, 70K for the verification agent (AGENT_DAILY_TOKEN_BUDGET), and 20K left
# over for smoke tests (see docs/decisions.md).
PIPELINE_DAILY_TOKEN_BUDGET = 110_000

# Why a lane call was not run.
SKIP_TIME = "time limit"
SKIP_PR_BUDGET = "PR token budget"
SKIP_DAILY_BUDGET = "daily token budget"


@dataclass
class OrchestratorResult:
    candidates: list[Candidate] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    failed_calls: int = 0
    skipped_calls: list[tuple[str, Chunk, str]] = field(default_factory=list)  # lane, chunk, why


def utc_today() -> date:
    return datetime.now(UTC).date()


async def run_lanes(
    groq: GroqClient,
    sessionmaker: async_sessionmaker,
    models: dict[str, str],
    repo_id: int,
    run_id: int,
    chunks: list[Chunk],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> OrchestratorResult:
    """Each (lane, chunk) call is answered from the cache if possible; otherwise it is sent
    to the LLM, unless the deadline or a budget says no.

    Calls start in priority order: source, then tests, then docs, and within each group the
    chunks with the most added lines first. So when time or budget runs out, what's left over
    is the least important. A failed call is counted and skipped; NonRetryableError (bad API
    key) stops everything.
    """
    ordered = sorted(chunks, key=lambda chunk: (priority(chunk.path), -chunk.added))
    pending = deque((lane, chunk) for chunk in ordered for lane in LANES)
    result = OrchestratorResult()
    usage = result.usage

    async def skip_reason(lane: str, chunk: Chunk, model: str) -> str | None:
        if clock() >= deadline:
            return SKIP_TIME
        estimate = estimate_call_tokens(lane, chunk)
        if usage.tokens_in + usage.tokens_out + estimate > PR_TOKEN_BUDGET:
            return SKIP_PR_BUDGET
        async with sessionmaker() as session:
            used_today = await daily_tokens_used(
                session, model, utc_today(), BudgetPurpose.PIPELINE
            )
        if used_today + estimate > PIPELINE_DAILY_TOKEN_BUDGET:
            return SKIP_DAILY_BUDGET
        return None

    async def record(delta: Usage) -> None:
        """Count a call in this attempt's usage and in the run's stored totals."""
        for name, value in asdict(delta).items():
            setattr(usage, name, getattr(usage, name) + value)
        async with sessionmaker() as session:
            await add_run_usage(session, run_id, delta)

    async def call_worker() -> None:
        while pending:
            lane, chunk = pending.popleft()
            model = models[lane]
            key = cache_key(repo_id, lane, model, chunk)

            # Cache first: a hit is free, so it's used even past the deadline or budget.
            async with sessionmaker() as session:
                cached = await get_cached(session, key)
            if cached:
                result.candidates.extend(from_cached(cached.findings, lane, chunk))
                # Saved only if another run paid for it; a retry reusing its own calls isn't.
                saved = cached.tokens_in + cached.tokens_out if cached.run_id != run_id else 0
                await record(Usage(cached_calls=1, tokens_saved=saved))
                continue

            reason = await skip_reason(lane, chunk, model)
            if reason:
                result.skipped_calls.append((lane, chunk, reason))
                continue
            try:
                lane_result = await run_lane(groq, model, lane, chunk, deadline)
            except DeadlineExceeded:
                result.skipped_calls.append((lane, chunk, SKIP_TIME))
                continue
            except LLMError as exc:
                result.failed_calls += 1
                logger.warning(
                    "lane call failed: %s",
                    exc,
                    extra={"fields": {"lane": lane, "file": chunk.path, "lines": chunk.start}},
                )
                continue

            tokens_in, tokens_out = lane_result.tokens_in, lane_result.tokens_out
            result.candidates.extend(lane_result.candidates)
            cost = cost_usd(model, tokens_in, tokens_out)
            await record(Usage(tokens_in, tokens_out, llm_calls=1, cost_usd=cost))
            # Saved right away, so a retry or takeover reuses this call even if the run dies.
            async with sessionmaker() as session:
                findings = to_cached(lane_result.candidates, chunk)
                await put_cached(session, key, findings, tokens_in, tokens_out, run_id)
                await add_daily_tokens(
                    session, model, utc_today(), BudgetPurpose.PIPELINE, tokens_in + tokens_out
                )

    try:
        # TaskGroup cancels the other workers if one raises (e.g. Groq rejects the key).
        async with asyncio.TaskGroup() as group:
            for _ in range(MAX_PARALLEL_CALLS):
                group.create_task(call_worker())
    except ExceptionGroup as errors:
        raise errors.exceptions[0] from None

    logger.info(
        "lanes finished",
        extra={
            "fields": {
                "calls": len(chunks) * len(LANES),
                "llm_calls": usage.llm_calls,
                "cached_calls": usage.cached_calls,
                "failed_calls": result.failed_calls,
                "skipped_calls": len(result.skipped_calls),
                "candidates": len(result.candidates),
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out,
                "tokens_saved": usage.tokens_saved,
                "cost_usd": str(usage.cost_usd),
            }
        },
    )
    return result


def nothing_reviewed_for_daily_budget(result: OrchestratorResult) -> bool:
    """True when the daily budget stopped every call and nothing came from the cache: there
    is nothing worth posting, so the job should wait for the budget to reset."""
    return (
        result.usage.llm_calls == 0
        and result.usage.cached_calls == 0
        and any(reason == SKIP_DAILY_BUDGET for _, _, reason in result.skipped_calls)
    )
