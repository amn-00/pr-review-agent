"""Run the verification agent over a run's findings: most severe first, two at a time, within
the agent's deadline and token budgets. Only findings it confirms get posted."""

import asyncio
import logging
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.loop import AgentStep, verify_finding
from app.agent.snapshot import download_snapshot
from app.agent.tools import AgentTools
from app.db.models import BudgetPurpose
from app.db.queries import (
    add_agent_step,
    add_daily_tokens,
    add_run_usage,
    daily_tokens_used,
    set_verdict,
)
from app.github.client import GitHubClient
from app.llm.groq import GroqClient
from app.llm.pricing import cost_usd
from app.pipeline.chunking import Chunk
from app.pipeline.lanes import Candidate
from app.pipeline.orchestrator import utc_today
from app.pipeline.usage import Usage

logger = logging.getLogger(__name__)

# The agent's share of GROQ_MODEL's daily tokens (the lanes get 110K; Groq allows 200K).
AGENT_DAILY_TOKEN_BUDGET = 70_000
# Agent tokens one attempt may spend, so one PR can't use up the day.
AGENT_PR_TOKEN_BUDGET = 30_000
# Two at a time: Groq's tokens-per-minute limit is the bottleneck, so more wouldn't help.
MAX_PARALLEL_AGENTS = 2
# What one finding usually costs (about 4.5K tokens in the real 4a run). A finding starts only
# if this much is left in both budgets, so they can overshoot by at most the findings in flight.
FINDING_ESTIMATE_TOKENS = 5_000
# A finding started with less time left would most likely end "uncertain (time limit)".
MIN_SECONDS_TO_START = 30

# Why a finding was not verified.
NOT_VERIFIED_TIME = "time limit"
NOT_VERIFIED_PR_BUDGET = "agent PR token budget"
NOT_VERIFIED_DAILY_BUDGET = "agent daily token budget"


@dataclass
class Check:
    finding_id: int
    candidate: Candidate
    verdict: str | None = None  # confirmed | dropped | uncertain; None = not verified
    reason: str = ""
    evidence: list[dict] = field(default_factory=list)


@dataclass
class AgentResult:
    model: str
    checks: list[Check]  # in the order the findings were given (most severe first)
    usage: Usage = field(default_factory=Usage)


async def verify_findings(
    groq: GroqClient,
    github: GitHubClient,
    sessionmaker: async_sessionmaker,
    model: str,
    installation_id: int,
    repo: str,
    sha: str,
    run_id: int,
    findings: list[tuple[int, Candidate]],
    chunks: list[Chunk],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Check each (finding_id, candidate), in order. Every step, its usage and every verdict
    are saved as they happen. NonRetryableError (bad API key) stops everything."""
    result = AgentResult(model, [Check(finding_id, c) for finding_id, c in findings])
    usage = result.usage
    pending = deque(result.checks)

    async def skip_reason() -> str | None:
        if deadline - clock() < MIN_SECONDS_TO_START:
            return NOT_VERIFIED_TIME
        if usage.tokens_in + usage.tokens_out + FINDING_ESTIMATE_TOKENS > AGENT_PR_TOKEN_BUDGET:
            return NOT_VERIFIED_PR_BUDGET
        async with sessionmaker() as session:
            used = await daily_tokens_used(session, model, utc_today(), BudgetPurpose.AGENT)
        if used + FINDING_ESTIMATE_TOKENS > AGENT_DAILY_TOKEN_BUDGET:
            return NOT_VERIFIED_DAILY_BUDGET
        return None

    async def save(check: Check) -> None:
        async with sessionmaker() as session:
            await set_verdict(
                session, check.finding_id, check.verdict, check.reason, check.evidence
            )

    # If no finding could start (e.g. the daily budget is used up), don't download the repo.
    reason = await skip_reason()
    if reason:
        for check in result.checks:
            check.reason = reason
            await save(check)
        _log(result)
        return result

    async def check_finding(check: Check, tools: AgentTools) -> None:
        async def on_step(step: AgentStep) -> None:
            cost = cost_usd(model, step.tokens_in, step.tokens_out)
            delta = Usage(step.tokens_in, step.tokens_out, llm_calls=1, cost_usd=cost)
            for name, value in asdict(delta).items():
                setattr(usage, name, getattr(usage, name) + value)
            async with sessionmaker() as session:
                await add_agent_step(session, check.finding_id, step)
                await add_run_usage(session, run_id, delta)
                await add_daily_tokens(
                    session,
                    model,
                    utc_today(),
                    BudgetPurpose.AGENT,
                    step.tokens_in + step.tokens_out,
                )

        chunk = _chunk_for(check.candidate, chunks)
        verdict = await verify_finding(
            groq, model, tools, check.candidate, chunk, deadline, on_step, clock
        )
        check.verdict, check.reason, check.evidence = (
            verdict.verdict,
            verdict.reason,
            verdict.evidence,
        )

    with tempfile.TemporaryDirectory() as folder:
        snapshot = await download_snapshot(github, installation_id, repo, sha, Path(folder))
        tools = AgentTools(snapshot, github, installation_id, repo, sha)

        async def agent_worker() -> None:
            while pending:
                check = pending.popleft()
                check.reason = await skip_reason() or ""
                if not check.reason:
                    await check_finding(check, tools)
                await save(check)

        try:
            # TaskGroup cancels the other worker if one raises (e.g. Groq rejects the key).
            async with asyncio.TaskGroup() as group:
                for _ in range(MAX_PARALLEL_AGENTS):
                    group.create_task(agent_worker())
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None

    _log(result)
    return result


def _chunk_for(candidate: Candidate, chunks: list[Chunk]) -> Chunk:
    """The chunk around the finding's first line. Aggregation keeps only findings whose first
    line is inside a hunk, and every hunk is inside a chunk, so there always is one."""
    return next(
        c for c in chunks if c.path == candidate.file and c.start <= candidate.line_start <= c.end
    )


def _log(result: AgentResult) -> None:
    verdicts: dict[str, int] = {}
    for check in result.checks:
        key = check.verdict or "not_verified"
        verdicts[key] = verdicts.get(key, 0) + 1
    logger.info(
        "agent finished",
        extra={
            "fields": {
                **verdicts,
                "llm_calls": result.usage.llm_calls,
                "tokens_in": result.usage.tokens_in,
                "tokens_out": result.usage.tokens_out,
                "cost_usd": str(result.usage.cost_usd),
            }
        },
    )
