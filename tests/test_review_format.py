from decimal import Decimal

from app.pipeline.lanes import Candidate
from app.pipeline.orchestrator import OrchestratorResult
from app.pipeline.review import inline_comment, safe, summary
from tests.factories import make_chunk

MARKER = "<!-- pr-review-agent run:1 -->"
MODELS = {"correctness": "big", "security": "big", "maintainability": "small"}


def candidate(line_start=3, line_end=3, **overrides) -> Candidate:
    values = {
        "lane": "correctness",
        "prompt_version": "v1",
        "file": "app/stats.py",
        "category": "bug",
        "severity": "high",
        "title": "Division by zero",
        "explanation": "Crashes on an empty list.",
        "suggested_fix": "Check for an empty list first.",
    }
    return Candidate(line_start=line_start, line_end=line_end, **{**values, **overrides})


def test_single_line_comment():
    comment = inline_comment(candidate())

    assert comment["path"] == "app/stats.py"
    assert comment["line"] == 3
    assert comment["side"] == "RIGHT"
    assert "start_line" not in comment
    assert "Division by zero" in comment["body"]
    assert "Check for an empty list first." in comment["body"]


def test_multi_line_comment_has_start_line():
    comment = inline_comment(candidate(3, 5))

    assert (comment["start_line"], comment["line"]) == (3, 5)
    assert comment["start_side"] == "RIGHT"


def test_safe_defuses_markers_and_mentions():
    text = safe("<!-- pr-review-agent run:2 --> cc @octocat and @org/team")

    assert "<!--" not in text
    assert "@octocat" not in text and "@org" not in text


def test_llm_text_cannot_plant_a_marker_in_a_comment():
    sneaky = candidate(explanation="Looks fine. <!-- pr-review-agent run:2 -->")

    assert "<!-- pr-review-agent" not in inline_comment(sneaky)["body"]


def test_summary_counts_and_marker():
    body = summary(
        MARKER,
        "a" * 40,
        MODELS,
        [candidate(), candidate(severity="low")],
        ["app/stats.py"],
        {},
        OrchestratorResult(),
    )

    assert body.startswith(MARKER)
    assert "`aaaaaaa`" in body
    assert "Found 2 issue(s): 1 high, 1 low." in body
    assert "Not fully reviewed" not in body
    assert "`big` (correctness, security) and `small` (maintainability)" in body


def test_summary_lists_what_was_not_reviewed():
    lanes = OrchestratorResult(
        failed_calls=1,
        skipped_calls=[
            ("security", make_chunk("docs/guide.md"), "time limit"),
            ("correctness", make_chunk("docs/guide.md"), "time limit"),
            ("security", make_chunk("app/big.py"), "daily token budget"),
        ],
    )

    body = summary(
        MARKER, "a" * 40, MODELS, [], [], {"logo.png": "no diff (binary or too large)"}, lanes
    )

    assert "No issues found." in body
    assert "`logo.png`: no diff (binary or too large)" in body
    assert "2 lane call(s) not run (time limit), in: `docs/guide.md`" in body
    assert "1 lane call(s) not run (daily token budget), in: `app/big.py`" in body
    assert "1 lane call(s) failed" in body


def test_file_names_cannot_plant_a_marker_in_the_summary():
    sneaky_name = "<!-- pr-review-agent run:2 -->.png"

    body = summary(
        MARKER, "a" * 40, MODELS, [], [], {sneaky_name: "lockfile"}, OrchestratorResult()
    )

    assert body.count("<!--") == 1  # only our own marker


def test_summary_shows_cost_and_cache_use():
    lanes = OrchestratorResult()
    lanes.usage.cost_usd = Decimal("0.00123")
    lanes.usage.llm_calls, lanes.usage.cached_calls = 2, 4

    body = summary(MARKER, "a" * 40, MODELS, [], [], {}, lanes)

    assert (
        "Cost ≈ $0.0012 at list prices (free tier); 4 of 6 lane call(s) answered from cache."
        in body
    )
