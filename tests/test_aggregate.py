from app.github.diff import Hunk
from app.pipeline.aggregate import aggregate
from app.pipeline.lanes import Candidate

HUNKS = {"a.py": [Hunk(10, 20, added={12, 13}), Hunk(40, 45, added={42})]}


def candidate(line_start=12, line_end=12, **overrides) -> Candidate:
    values = {
        "lane": "correctness",
        "prompt_version": "v1",
        "file": "a.py",
        "category": "bug",
        "severity": "medium",
        "title": "t",
        "explanation": "e",
        "suggested_fix": None,
    }
    return Candidate(line_start=line_start, line_end=line_end, **{**values, **overrides})


def kept(result):
    return [c for c in result if c.drop_reason is None]


def test_lines_outside_the_diff_are_dropped():
    outside = candidate(30, 31)
    other_file = candidate(file="b.py")

    result = aggregate([outside, other_file], HUNKS)

    assert kept(result) == []
    assert {c.drop_reason for c in result} == {"not_in_diff"}


def test_range_leaving_its_hunk_shrinks_to_its_first_line():
    spans_hunks = candidate(18, 42)

    [result] = aggregate([spans_hunks], HUNKS)

    assert result.drop_reason is None
    assert (result.line_start, result.line_end) == (18, 18)


def test_reversed_range_is_fixed():
    [result] = aggregate([candidate(14, 12)], HUNKS)

    assert (result.line_start, result.line_end) == (12, 14)


def test_overlapping_same_category_keeps_the_most_severe():
    low = candidate(12, 13, severity="low")
    high = candidate(13, 14, severity="high")

    result = aggregate([low, high], HUNKS)

    assert kept(result) == [high]
    assert low.drop_reason == "duplicate"


def test_overlap_across_lanes_keeps_only_the_most_severe():
    # PR #4: correctness flagged a critical IndexError, maintainability a low idiom note on
    # overlapping lines. Only the critical one should be posted.
    bug = candidate(12, 14, category="bug", severity="critical")
    idiom = candidate(13, 15, category="idiom", severity="low", lane="maintainability")

    result = aggregate([idiom, bug], HUNKS)

    assert kept(result) == [bug]
    assert idiom.drop_reason == "duplicate"


def test_severity_tie_across_lanes_goes_by_lane_order():
    readability = candidate(12, 12, category="readability", lane="maintainability")
    security = candidate(12, 12, category="security", lane="security")
    bug = candidate(12, 12, category="bug", lane="correctness")

    result = aggregate([readability, security, bug], HUNKS)

    assert kept(result) == [bug]


def test_kept_findings_are_ranked_by_severity_then_position():
    a = candidate(42, 42, severity="low")
    b = candidate(15, 15, severity="critical")
    c = candidate(12, 12, severity="low", category="performance")

    result = aggregate([a, b, c], HUNKS)

    assert kept(result) == [b, c, a]
