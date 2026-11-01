"""Decide which candidates get posted: drop invented lines, merge duplicates, rank."""

from app.github.diff import Hunk, hunk_for_line
from app.pipeline.lanes import LANES, SEVERITIES, Candidate


def aggregate(candidates: list[Candidate], hunks_by_file: dict[str, list[Hunk]]) -> list[Candidate]:
    """Returns every candidate: the ones to post first (ranked), then the dropped ones with
    `drop_reason` set. Candidates may be adjusted in place (line range shrunk)."""
    kept: list[Candidate] = []
    dropped: list[Candidate] = []

    # Most severe first, so dedupe keeps the most severe of overlapping candidates. On a tie,
    # lane order decides: correctness, then security, then maintainability.
    by_importance = sorted(
        candidates, key=lambda c: (SEVERITIES.index(c.severity), LANES.index(c.lane))
    )
    for candidate in by_importance:
        if not _fit_to_diff(candidate, hunks_by_file.get(candidate.file, [])):
            candidate.drop_reason = "not_in_diff"
            dropped.append(candidate)
            continue
        # `kept` is still in order of importance here, so this is the most severe overlap.
        winner = next((other for other in kept if _same_issue(candidate, other)), None)
        if winner:
            candidate.drop_reason = "duplicate"
            candidate.duplicate_of = winner
            dropped.append(candidate)
        else:
            kept.append(candidate)

    kept.sort(key=lambda c: (SEVERITIES.index(c.severity), c.file, c.line_start))
    return kept + dropped


def _fit_to_diff(candidate: Candidate, hunks: list[Hunk]) -> bool:
    """Hallucination guard. GitHub only accepts comments on lines inside one hunk, so the
    first line must be in a hunk; a range that leaves that hunk shrinks to its first line."""
    if candidate.line_end < candidate.line_start:
        candidate.line_start, candidate.line_end = candidate.line_end, candidate.line_start
    hunk = hunk_for_line(hunks, candidate.line_start)
    if hunk is None:
        return False
    if not hunk.contains(candidate.line_end):
        candidate.line_end = candidate.line_start
    return True


def _same_issue(a: Candidate, b: Candidate) -> bool:
    """Same file, overlapping lines, whatever the lane or category: two comments on the same
    lines are noise, so only the most severe is posted (see docs/decisions.md)."""
    return a.file == b.file and a.line_start <= b.line_end and b.line_start <= a.line_end
