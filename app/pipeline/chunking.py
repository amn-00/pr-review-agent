"""Turn changed files into chunks small enough for one LLM call each."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.github.diff import Hunk
from app.static.findings import ToolFinding

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
    # The same lines without numbers, for the cache key: a hunk that only moved still matches.
    body: str
    added: int  # how many lines this PR added in the chunk (more = reviewed earlier)
    # Static-tool findings on this chunk's added lines, by the lane that sees them.
    hints: dict[str, list[ToolFinding]] = field(default_factory=dict)


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
        marked = [("+" if n in added else " ", lines[n - 1]) for n in range(start, end + 1)]
        chunks.extend(_split(path, start, marked))
    return chunks


def _split(path: str, start: int, marked: list[tuple[str, str]]) -> list[Chunk]:
    """Cut a window of (marker, code) lines into chunks of at most MAX_CHUNK_TOKENS, on line
    boundaries."""
    chunks: list[Chunk] = []
    piece: list[tuple[str, str]] = []
    piece_start = start
    for offset, line in enumerate(marked):
        if piece and estimate_tokens(_numbered(piece_start, [*piece, line])) > MAX_CHUNK_TOKENS:
            chunks.append(_chunk(path, piece_start, piece))
            piece, piece_start = [], start + offset
        piece.append(line)
    if piece:
        chunks.append(_chunk(path, piece_start, piece))
    return chunks


def _numbered(start: int, marked: list[tuple[str, str]]) -> str:
    return "\n".join(f"{start + i:>5} {mark} {code}" for i, (mark, code) in enumerate(marked))


def _chunk(path: str, start: int, marked: list[tuple[str, str]]) -> Chunk:
    return Chunk(
        path=path,
        start=start,
        end=start + len(marked) - 1,
        text=_numbered(start, marked),
        body="\n".join(f"{mark} {code}" for mark, code in marked),
        added=sum(mark == "+" for mark, _ in marked),
    )
