"""The three lanes: plain LLM calls (not agents), one per (lane, chunk)."""

import secrets
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.llm.groq import GroqClient, LLMError
from app.pipeline.chunking import Chunk

PROMPT_VERSION = "v1"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / PROMPT_VERSION

CATEGORIES_BY_LANE = {
    "correctness": ["bug", "performance"],
    "security": ["security"],
    "maintainability": ["readability", "idiom"],
}
LANES = list(CATEGORIES_BY_LANE)
SEVERITIES = ["critical", "high", "medium", "low"]


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


@cache
def system_prompt(lane: str) -> str:
    common = (PROMPTS_DIR / "common.md").read_text(encoding="utf-8")
    return common + "\n" + (PROMPTS_DIR / f"{lane}.md").read_text(encoding="utf-8")


def user_message(chunk: Chunk, boundary: str) -> str:
    """The chunk wrapped in markers with a random ID, so content in the PR can't fake the
    end marker and put text outside the untrusted block. The file name is PR content too."""
    return (
        "Review the file excerpt below. Numbers on the left are new-file line numbers; "
        '"+" marks lines added or changed in this pull request.\n\n'
        f"BEGIN UNTRUSTED PR CONTENT {boundary}\n"
        f"File: {chunk.path}\n"
        f"{chunk.text}\n"
        f"END UNTRUSTED PR CONTENT {boundary}"
    )


async def run_lane(
    groq: GroqClient, model: str, lane: str, chunk: Chunk, deadline: float
) -> LaneResult:
    """Raises LLMError for a failed call or output that doesn't match the schema."""
    messages = [
        {"role": "system", "content": system_prompt(lane)},
        {"role": "user", "content": user_message(chunk, boundary=secrets.token_hex(8))},
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
