"""Lane results cached by chunk content (spec 5.4), so unchanged chunks cost nothing on a new
push, and a retry doesn't pay for calls an earlier attempt already made."""

import hashlib

from app.pipeline.chunking import Chunk
from app.pipeline.lanes import PROMPT_VERSION, Candidate, LaneFinding, system_prompt


def cache_key(repo_id: int, lane: str, model: str, chunk: Chunk) -> str:
    """Same key means the model would see exactly the same request (minus line numbers).

    - The prompt text (not just its version) and the model: a changed prompt or model never
      reuses another's answers.
    - The chunk without line numbers: a hunk that only moved down the file still hits.
    - The repo: findings can quote code, so they're never shared across repos.
    """
    parts = [str(repo_id), lane, system_prompt(lane), model, chunk.path, chunk.body]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def to_cached(candidates: list[Candidate], chunk: Chunk) -> list[dict]:
    """The model's findings, with line numbers relative to the chunk's first line."""
    return [
        {
            **{name: getattr(c, name) for name in LaneFinding.model_fields},
            "line_start": c.line_start - chunk.start,
            "line_end": c.line_end - chunk.start,
        }
        for c in candidates
    ]


def from_cached(findings: list[dict], lane: str, chunk: Chunk) -> list[Candidate]:
    """Cached findings placed at the chunk's current position."""
    return [
        Candidate(
            lane=lane,
            prompt_version=PROMPT_VERSION,
            file=chunk.path,
            **{
                **finding,
                "line_start": finding["line_start"] + chunk.start,
                "line_end": finding["line_end"] + chunk.start,
            },
        )
        for finding in findings
    ]
