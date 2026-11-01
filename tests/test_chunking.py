import pytest

from app.github.diff import Hunk
from app.pipeline.chunking import (
    CONTEXT_LINES,
    MAX_CHUNK_TOKENS,
    build_chunks,
    estimate_tokens,
    priority,
    skip_reason,
)


def file_with(lines: int) -> str:
    return "\n".join(f"line {n}" for n in range(1, lines + 1))


def test_chunk_shows_context_around_the_hunk_with_numbers_and_markers():
    [chunk] = build_chunks("a.py", [Hunk(50, 52, added={51})], file_with(200))

    assert (chunk.start, chunk.end) == (50 - CONTEXT_LINES, 52 + CONTEXT_LINES)
    rendered = chunk.text.splitlines()
    assert rendered[0] == "   30   line 30"
    assert "   51 + line 51" in rendered


def test_context_is_clipped_at_file_edges():
    [chunk] = build_chunks("a.py", [Hunk(2, 3, added={2})], file_with(10))

    assert (chunk.start, chunk.end) == (1, 10)


def test_hunks_with_overlapping_context_are_merged():
    hunks = [Hunk(50, 50, added={50}), Hunk(80, 80, added={80})]  # 30 lines apart

    chunks = build_chunks("a.py", hunks, file_with(200))

    assert [(c.start, c.end) for c in chunks] == [(30, 100)]


def test_distant_hunks_get_separate_chunks():
    hunks = [Hunk(50, 50, added={50}), Hunk(150, 150, added={150})]

    chunks = build_chunks("a.py", hunks, file_with(200))

    assert [(c.start, c.end) for c in chunks] == [(30, 70), (130, 170)]


def test_big_chunks_are_split_on_line_boundaries():
    long_lines = "\n".join("x" * 200 for _ in range(300))

    chunks = build_chunks("a.py", [Hunk(1, 300, added=set(range(1, 301)))], long_lines)

    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= MAX_CHUNK_TOKENS for c in chunks)
    # Pieces are contiguous and cover every line exactly once.
    assert chunks[0].start == 1 and chunks[-1].end == 300
    assert all(a.end + 1 == b.start for a, b in zip(chunks, chunks[1:], strict=False))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/worker.py", 0),
        ("src/index.ts", 0),
        ("tests/test_worker.py", 1),
        ("app/test_utils.py", 1),
        ("pkg/server_test.py", 1),
        ("web/button.test.tsx", 1),
        ("web/button.spec.js", 1),
        ("README.md", 2),
        ("docs/setup.py", 2),
        ("notes.txt", 2),
    ],
)
def test_priority_puts_source_before_tests_before_docs(path, expected):
    assert priority(path) == expected


@pytest.mark.parametrize(
    ("file", "reason"),
    [
        ({"filename": "a.py", "status": "removed", "patch": "@@"}, "deleted"),
        ({"filename": "web/package-lock.json", "status": "modified", "patch": "@@"}, "lockfile"),
        ({"filename": "logo.png", "status": "added"}, "no diff (binary or too large)"),
        ({"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}, None),
    ],
)
def test_skip_reason(file, reason):
    assert skip_reason(file) == reason
