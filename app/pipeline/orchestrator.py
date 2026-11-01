"""Run every lane over every chunk, a few calls at a time, until done or out of time."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.llm.groq import DeadlineExceeded, GroqClient, LLMError
from app.pipeline.chunking import Chunk, priority
from app.pipeline.lanes import LANES, Candidate, run_lane

logger = logging.getLogger(__name__)

# Calls in flight at once. More doesn't help: Groq's tokens-per-minute limit is the bottleneck.
MAX_PARALLEL_CALLS = 3


@dataclass
class OrchestratorResult:
    candidates: list[Candidate] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    failed_calls: int = 0
    # (lane, chunk) pairs never run because the deadline came first
    skipped_calls: list[tuple[str, Chunk]] = field(default_factory=list)


async def run_lanes(
    groq: GroqClient,
    model: str,
    chunks: list[Chunk],
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> OrchestratorResult:
    """Calls start in priority order (source, then tests, then docs), all lanes of a chunk
    together, so the time limit cuts off the least important chunks.

    A failed call is counted and skipped. NonRetryableError (bad API key) stops everything.
    """
    ordered = sorted(chunks, key=lambda chunk: priority(chunk.path))  # stable: keeps diff order
    pending = deque((lane, chunk) for chunk in ordered for lane in LANES)
    result = OrchestratorResult()

    async def call_worker() -> None:
        while pending:
            lane, chunk = pending.popleft()
            if clock() >= deadline:
                result.skipped_calls.append((lane, chunk))
                continue
            try:
                lane_result = await run_lane(groq, model, lane, chunk, deadline)
            except DeadlineExceeded:
                result.skipped_calls.append((lane, chunk))
            except LLMError as exc:
                result.failed_calls += 1
                logger.warning(
                    "lane call failed: %s",
                    exc,
                    extra={"fields": {"lane": lane, "file": chunk.path, "lines": chunk.start}},
                )
            else:
                result.candidates.extend(lane_result.candidates)
                result.tokens_in += lane_result.tokens_in
                result.tokens_out += lane_result.tokens_out

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
                "candidates": len(result.candidates),
                "failed_calls": result.failed_calls,
                "skipped_calls": len(result.skipped_calls),
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            }
        },
    )
    return result
