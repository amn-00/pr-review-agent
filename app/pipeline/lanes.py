"""The three lanes: plain LLM calls (not agents), one per (lane, chunk)."""

import secrets
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.llm.groq import EXPECTED_OUTPUT_TOKENS, GroqClient, LLMError
from app.pipeline.chunking import Chunk, estimate_tokens

PROMPT_VERSION = "v3"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / PROMPT_VERSION

CATEGORIES_BY_LANE = {
    "correctness": ["bug", "performance"],
    "security": ["security"],
    "maintainability": ["readability", "idiom"],
}
LANES = list(CATEGORIES_BY_LANE)
# Style findings: shown in the review summary's collapsed "Suggestions" section, never as inline
# comments. Inline comments are for behaviour findings (bug, performance, security).
SUGGESTION_CATEGORIES = set(CATEGORIES_BY_LANE["maintainability"])
SEVERITIES = ["critical", "high", "medium", "low"]
# Lanes run on the small model (GROQ_SMALL_MODEL); the rest on the main one (GROQ_MODEL).
# Maintainability is the lowest-stakes lane, so it gives up the stronger model.
SMALL_MODEL_LANES = {"maintainability"}


class LaneFinding(BaseModel):
    """One finding as the model returns it. The file comes from the chunk, not the model."""

    line_start: int
    line_end: int
    category: Literal["bug", "performance", "security", "readability", "idiom"]
    severity: Literal["critical", "high", "medium", "low"]
    title: str
    explanation: str
    suggested_fix: str | None


class LaneOutput(BaseModel):
    findings: list[LaneFinding]


@dataclass
class Candidate:
    """A finding from a lane, before aggregation decides whether it is posted."""

    lane: str
    prompt_version: str
    file: str
    line_start: int
    line_end: int
    category: str
    severity: str
    title: str
    explanation: str
    suggested_fix: str | None
    drop_reason: str | None = None  # set by aggregate: "not_in_diff" or "duplicate"
    # Set by aggregate on a duplicate: the candidate that beat it (stored as its finding ID).
    duplicate_of: "Candidate | None" = field(default=None, compare=False, repr=False)


@dataclass
class LaneResult:
    candidates: list[Candidate]
    tokens_in: int
    tokens_out: int


def lane_schema(lane: str) -> dict:
    """JSON schema for Groq's strict structured output. Strict mode needs every property in
    `required` and `additionalProperties: false`; the optional fix is a string-or-null."""
    finding = {
        "type": "object",
        "properties": {
            "line_start": {"type": "integer"},
            "line_end": {"type": "integer"},
            "category": {"type": "string", "enum": CATEGORIES_BY_LANE[lane]},
            "severity": {"type": "string", "enum": SEVERITIES},
            "title": {"type": "string"},
            "explanation": {"type": "string"},
            "suggested_fix": {"type": ["string", "null"]},
        },
        "required": list(LaneFinding.model_fields),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": finding}},
        "required": ["findings"],
        "additionalProperties": False,
    }


def lane_models(main_model: str, small_model: str) -> dict[str, str]:
    return {lane: small_model if lane in SMALL_MODEL_LANES else main_model for lane in LANES}


def estimate_call_tokens(lane: str, chunk: Chunk) -> int:
    """Tokens one call will probably use (prompt plus typical output), for budget checks."""
    prompt = system_prompt(lane) + chunk.text + tool_hints(chunk, lane)
    return estimate_tokens(prompt) + EXPECTED_OUTPUT_TOKENS


@cache
def system_prompt(lane: str) -> str:
    common = (PROMPTS_DIR / "common.md").read_text(encoding="utf-8")
    return common + "\n" + (PROMPTS_DIR / f"{lane}.md").read_text(encoding="utf-8")


def tool_hints(chunk: Chunk, lane: str) -> str:
    """The static-tool findings this lane should see, as text ("" if none). Part of the cache
    key too: the same code with different tool output may get a different answer."""
    hints = chunk.hints.get(lane, [])
    if not hints:
        return ""
    lines = [f"- line {h.line}: {h.code} {h.message} ({h.tool})" for h in hints]
    return "Tool findings on changed lines (hints to verify, not conclusions):\n" + "\n".join(lines)


def user_message(chunk: Chunk, boundary: str, lane: str) -> str:
    """The chunk wrapped in markers with a random ID, so content in the PR can't fake the
    end marker and put text outside the untrusted block. The file name is PR content too,
    and so are tool findings: they quote the code."""
    hints = tool_hints(chunk, lane)
    return (
        "Review the file excerpt below. Numbers on the left are new-file line numbers; "
        '"+" marks lines added or changed in this pull request.\n\n'
        f"BEGIN UNTRUSTED PR CONTENT {boundary}\n"
        f"File: {chunk.path}\n"
        f"{chunk.text}\n"
        + (f"\n{hints}\n" if hints else "")
        + f"END UNTRUSTED PR CONTENT {boundary}"
    )


async def run_lane(
    groq: GroqClient, model: str, lane: str, chunk: Chunk, deadline: float
) -> LaneResult:
    """Raises LLMError for a failed call or output that doesn't match the schema."""
    messages = [
        {"role": "system", "content": system_prompt(lane)},
        {"role": "user", "content": user_message(chunk, secrets.token_hex(8), lane)},
    ]
    completion = await groq.complete_json(
        model, messages, f"{lane}_findings", lane_schema(lane), deadline
    )
    try:
        output = LaneOutput.model_validate_json(completion.content)
    except ValidationError as exc:
        raise LLMError(f"{lane} lane returned invalid output: {exc}") from exc

    candidates = [
        Candidate(lane=lane, prompt_version=PROMPT_VERSION, file=chunk.path, **f.model_dump())
        for f in output.findings
        if f.category in CATEGORIES_BY_LANE[lane]
    ]
    return LaneResult(candidates, completion.tokens_in, completion.tokens_out)
