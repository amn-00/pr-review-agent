"""Turn changed files into chunks small enough for one LLM call each."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from app.github.diff import Hunk

# Lines of unchanged code shown above and below each hunk, taken from the full file.
CONTEXT_LINES = 20
MAX_CHUNK_TOKENS = 2000
# Generated files: reviewing them costs tokens and finds nothing.
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock"}
DOC_SUFFIXES = {".md", ".rst", ".txt"}


@dataclass
class Chunk:
    path: str
    start: int  # first line shown (new-file numbering)
    end: int  # last line shown
    text: str  # numbered lines, "+" marking lines added in this PR


def estimate_tokens(text: str) -> int:
    """About 4 characters per token for code. Good enough for sizing; no tokenizer needed."""
    return len(text) // 4 + 1


def skip_reason(file: dict) -> str | None:
    """Why a changed file from GitHub's PR files API isn't reviewed, or None to review it."""
    if file["status"] == "removed":
        return "deleted"
    if PurePosixPath(file["filename"]).name in LOCKFILES:
        return "lockfile"
    if not file.get("patch"):
        return "no diff (binary or too large)"
    return None


def priority(path: str) -> int:
    """Lower is reviewed first: source code, then tests, then docs. When the time limit
    hits, the chunks left over are the least important ones."""
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    if PurePosixPath(name).suffix in DOC_SUFFIXES or "docs" in parts:
        return 2
    is_test = (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )
    return 1 if is_test else 0


def build_chunks(path: str, hunks: list[Hunk], content: str) -> list[Chunk]:
    """One chunk per hunk plus CONTEXT_LINES around it. Hunks whose context overlaps are
    merged, and a chunk over MAX_CHUNK_TOKENS is split."""
    lines = content.splitlines()
    added = set().union(*(hunk.added for hunk in hunks))

    windows: list[list[int]] = []
    for hunk in hunks:
        start = max(1, hunk.start - CONTEXT_LINES)
        end = min(len(lines), hunk.end + CONTEXT_LINES)
        if windows and start <= windows[-1][1] + 1:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])

    chunks: list[Chunk] = []
    for start, end in windows:
        numbered = [
            f"{n:>5} {'+' if n in added else ' '} {lines[n - 1]}" for n in range(start, end + 1)
        ]
        chunks.extend(_split(path, start, numbered))
    return chunks


def _split(path: str, start: int, numbered: list[str]) -> list[Chunk]:
    """Cut a window into pieces of at most MAX_CHUNK_TOKENS, on line boundaries."""
    chunks: list[Chunk] = []
    piece: list[str] = []
    piece_start = start
    for offset, line in enumerate(numbered):
        if piece and estimate_tokens("\n".join([*piece, line])) > MAX_CHUNK_TOKENS:
            chunks.append(Chunk(path, piece_start, start + offset - 1, "\n".join(piece)))
            piece, piece_start = [], start + offset
        piece.append(line)
    if piece:
        chunks.append(Chunk(path, piece_start, start + len(numbered) - 1, "\n".join(piece)))
    return chunks
