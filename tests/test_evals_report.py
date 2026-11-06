"""Scoring and the known-issue analyses in the eval report, on hand-built results."""

import json

import pytest

from evals.dataset import Case, Label
from evals.report import (
    calls_cost,
    corroborated,
    fisher_one_sided,
    load_results,
    render,
    score,
    sign_test,
    truth,
    wilson,
)

BUG = Label("a.py", (10, 10), "the bug", ("bug",))
SECURITY = Label("a.py", (10, 10), "the injection", ("security",))
TRAP = Label(
    "a.py", (20, 20), "the trap", ("bug", "performance", "security", "readability", "idiom")
)
OK = Label("a.py", (30, 30), "real but not planted", ("bug", "readability"))


def finding(id, line, lane="correctness", category="bug", **fields) -> dict:
    return {
        "id": id,
        "lane": lane,
        "file": "a.py",
        "line_start": line,
        "line_end": line,
        "category": category,
        "severity": "high",
        "title": f"finding {id}",
        "drop_reason": None,
        "duplicate_of": None,
        "verdict": None,
        "verdict_reason": None,
        "posted": False,
        "agent_steps": [],
        **fields,
    }


def make_case(expect=(BUG,), traps=(), acceptable=()) -> Case:
    head = {"a.py": "x\n" * 40}
    return Case("c", "bug", "t", {}, head, None, list(expect), list(traps), list(acceptable), [])


def result(case: Case, mode: str, findings: list[dict], tool_findings=()) -> dict:
    return {
        "case": case.id,
        "mode": mode,
        "status": "complete",
        "head_sha": case.target(mode)[1],
        "problems": [],
        "notes": [],
        "wall_seconds": 10.0,
        "run": {},
        "calls": [],
        "earlier_calls": [],
        "tool_findings": list(tool_findings),
        "findings": findings,
    }


def report(case: Case, pipeline: list[dict], agent: list[dict], tool_findings=()) -> str:
    results = {
        (case.id, "pipeline"): result(case, "pipeline", pipeline, tool_findings),
        (case.id, "agent"): result(case, "agent", agent, tool_findings),
    }
    return render("t", [case], results)


def test_score_counts_catches_false_positives_and_traps():
    case = make_case(traps=[TRAP], acceptable=[OK])
    findings = [
        finding(1, 10, posted=True),  # catches the bug
        finding(2, 10, category="readability", posted=True),  # right line, wrong category: FP
        finding(3, 20, posted=True),  # the trap: FP
        finding(4, 30, posted=True),  # acceptable: neither
        finding(5, 25),  # not posted: doesn't count
    ]

    s = score(case, findings)

    assert [(label.note, f["id"]) for label, f in s.caught] == [("the bug", 1)]
    assert s.missed == []
    assert s.true_positives == 1
    assert [f["id"] for f in s.false_positives] == [2, 3]
    assert s.traps_posted == [TRAP]
    assert [truth(case, f) for f in findings[:4]] == ["bug", "false positive", "trap", "acceptable"]


def test_two_bugs_on_one_line_need_two_findings():
    case = make_case(expect=[BUG, SECURITY])

    s = score(case, [finding(1, 10, lane="security", category="security", posted=True)])

    assert [label.note for label, _ in s.caught] == ["the injection"]
    assert s.missed == [BUG]


def test_cross_lane_dedupe_loss_is_reported():
    # Security won on line 10; correctness's bug on the same line was dropped as its duplicate.
    case = make_case(expect=[BUG, SECURITY])
    pipeline = [
        finding(1, 10, lane="security", category="security", posted=True),
        finding(2, 10, drop_reason="duplicate", duplicate_of=1),
    ]

    text = report(case, pipeline, pipeline)

    assert "1 duplicate(s) dropped by aggregate, 1 of them across lanes." in text
    lost = "`a.py:10` correctness/high: finding 2"
    winner = "`a.py:10` security/high: finding 1"
    assert f"| c | the bug | {lost} | {winner} | **no** |" in text


def test_duplicate_of_a_dropped_winner_is_reported():
    case = make_case()
    agent = [
        finding(1, 10, lane="security", category="security", verdict="dropped"),
        finding(2, 10, drop_reason="duplicate", duplicate_of=1),
    ]

    text = report(case, agent, agent)

    assert "1 duplicate(s) whose winner the agent didn't confirm" in text
    assert "| c | `a.py:10` correctness/high: finding 2 | bug | dropped |" in text


def test_corroboration_needs_the_same_lane_and_a_line_in_range():
    tool = {"file": "a.py", "line": 10, "lane": "security", "tool": "semgrep", "code": "x"}

    assert corroborated(finding(1, 10, lane="security"), [tool])
    assert not corroborated(finding(1, 10, lane="correctness"), [tool])
    assert not corroborated(finding(1, 11, lane="security"), [tool])
    assert corroborated(finding(1, 9, lane="security", line_end=10), [tool])


def test_corroborated_false_positive_shows_what_a_skip_would_post():
    case = make_case(traps=[TRAP])
    steps = [{"tokens_in": 900, "tokens_out": 100, "tool": "find_callers", "args_json": {}}]
    agent = [
        finding(1, 20, lane="security", category="security", verdict="dropped", agent_steps=steps)
    ]
    tool = {"file": "a.py", "line": 20, "lane": "security", "tool": "semgrep", "code": "sql"}

    text = report(case, agent, agent, [tool])

    assert "1 finding(s) had a tool finding on their lines; the agent spent 1000 tokens" in text
    assert "| c | `a.py:20` security/high: finding 1 | trap | dropped |" in text


def test_cost_counts_earlier_attempts_and_splits_lanes_from_the_agent():
    calls = [
        {
            "kind": "correctness",
            "model": "openai/gpt-oss-120b",
            "tokens_in": 1_000_000,
            "tokens_out": 0,
        },
        {"kind": "agent", "model": "openai/gpt-oss-120b", "tokens_in": 0, "tokens_out": 1_000_000},
    ]
    earlier = [
        {
            "kind": "security",
            "model": "openai/gpt-oss-120b",
            "tokens_in": 1_000_000,
            "tokens_out": 0,
        }
    ]
    run = {"calls": calls, "earlier_calls": earlier}

    assert calls_cost(run, agent=False) == 0.30  # 2M tokens in at $0.15/M
    assert calls_cost(run, agent=True) == 0.60  # 1M tokens out at $0.60/M


# --- comparing two agents on the same candidates ---


def candidates() -> list[dict]:
    """What the lanes found, the same in both evals: the bug (line 10) and the trap (20)."""
    return [finding(1, 10), finding(2, 20, lane="security", category="security")]


def judged(bug_verdict: str, trap_verdict: str) -> list[dict]:
    """The candidates after an agent: only confirmed findings are posted."""
    found = candidates()
    for f, verdict in zip(found, (bug_verdict, trap_verdict), strict=True):
        f |= {
            "verdict": verdict,
            "verdict_reason": f"{verdict} because",
            "posted": verdict == "confirmed",
        }
    return found


def compared(
    bug_verdict: str, trap_verdict: str, baseline_bug="dropped", baseline_trap="dropped"
) -> str:
    """agent-v2's report against a baseline (by default, one that dropped the bug and the
    trap)."""
    case = make_case(traps=[TRAP])
    v2 = result(case, "agent", judged(bug_verdict, trap_verdict))
    v2["agent_prompt_version"] = "v2"
    results = {
        (case.id, "pipeline"): result(case, "pipeline", candidates()),
        (case.id, "agent"): v2,
    }
    old = judged(baseline_bug, baseline_trap)
    baseline = {(case.id, "agent"): result(case, "agent", old)}
    return render("agent-v2", [case], results, ("baseline", baseline))


def test_comparison_puts_both_agents_side_by_side():
    text = compared("confirmed", "dropped")

    columns = [
        "Pipeline only",
        "Pipeline + agent (baseline, agent v1)",
        "Pipeline + agent (agent v2)",
    ]
    assert f"| Metric | {' | '.join(columns)} |" in text
    assert "| Bugs caught | 0/1 (0%) | 0/1 (0%) | 1/1 (100%) |" in text
    assert "| Traps posted | 0/1 | 0/1 | 0/1 |" in text


def test_over_correction_check_passes_when_only_bugs_caught_go_up():
    text = compared("confirmed", "dropped")

    assert (
        "bugs caught +1, traps posted +0, clean PRs with a false positive +0, "
        "false positives +0." in text
    )
    assert "**passes**" in text


def test_over_correction_check_fails_when_a_trap_comes_back():
    text = compared("confirmed", "confirmed")

    assert "traps posted +1" in text and "false positives +1" in text
    assert "**doesn't pass**" in text


def test_over_correction_check_passes_on_fewer_false_positives_alone():
    # Same bugs, and the trap the baseline posted is now dropped.
    text = compared("dropped", "dropped", baseline_bug="dropped", baseline_trap="confirmed")

    assert "bugs caught +0, traps posted -1" in text and "false positives -1." in text
    assert "**passes**" in text


def test_over_correction_check_needs_something_strictly_better():
    text = compared("confirmed", "dropped", baseline_bug="confirmed", baseline_trap="dropped")

    assert "bugs caught +0, traps posted +0" in text and "false positives +0." in text
    assert "**doesn't pass**" in text


def test_over_correction_check_fails_on_fewer_bugs_even_with_fewer_false_positives():
    text = compared("dropped", "dropped", baseline_bug="confirmed", baseline_trap="confirmed")

    assert "bugs caught -1" in text and "false positives -1." in text
    assert "**doesn't pass**" in text


def test_a_trap_no_lane_reported_is_flagged_as_testing_nothing():
    case = make_case(traps=[TRAP])  # the trap is on line 20

    untested = report(case, [finding(1, 10, posted=True)], [])
    tested = report(
        case,
        [finding(1, 10, posted=True), finding(2, 20, drop_reason="duplicate", duplicate_of=1)],
        [],
    )

    assert "**Traps that test nothing.**" in untested
    assert "- `c`: the trap" in untested
    assert "**Traps that test nothing.**" not in tested  # a lane reported it, even if dropped


def test_changed_verdicts_are_listed_with_both_sides():
    text = compared("confirmed", "dropped")

    finding_1 = "`a.py:10` correctness/high: finding 1"
    assert f"| c | {finding_1} | bug | dropped | confirmed: confirmed because |" in text
    assert "finding 2 |" not in text.split("## Changed verdicts")[1].split("##")[0]  # unchanged


def test_a_tuned_agent_says_so_in_the_limits():
    tuned, baseline = compared("confirmed", "dropped"), report(make_case(), [], [])

    assert "**Tuned on the test set.**" in tuned
    assert "**Tuned on the test set.**" not in baseline


def test_held_out_cases_say_so_instead():
    case = make_case()
    agent = result(case, "agent", []) | {"agent_prompt_version": "v3"}
    results = {(case.id, "pipeline"): result(case, "pipeline", []), (case.id, "agent"): agent}

    text = render("heldout", [case], results, held_out=True)

    assert "**Held-out cases.**" in text
    assert "**Tuned on the test set.**" not in text


# --- inline comments vs suggestions ---


def test_every_run_is_scored_by_the_posting_rule(tmp_path):
    case = make_case()
    style = {"lane": "maintainability", "category": "readability"}
    # Saved before the rule existed: the style finding was posted inline then.
    pipeline = result(
        case,
        "pipeline",
        [finding(1, 10, posted=True), finding(2, 12, posted=True, **style), finding(3, 14)],
    )
    agent = result(
        case,
        "agent",
        [
            finding(1, 10, verdict="confirmed", posted=True),
            finding(2, 12, verdict="confirmed", posted=True, **style),
            finding(3, 14, verdict="dropped"),
            finding(4, 16, drop_reason="duplicate", duplicate_of=1),
        ],
    )
    for r in (pipeline, agent):
        (tmp_path / f"c.{r['mode']}.json").write_text(json.dumps(r), encoding="utf-8")

    results = load_results(tmp_path)

    shown = {
        mode: [(f["id"], f["posted"], f["suggestion"]) for f in results[("c", mode)]["findings"]]
        for mode in ("pipeline", "agent")
    }
    # Without the agent everything kept is shown; with it, only what it confirmed.
    assert shown["pipeline"] == [(1, True, False), (2, False, True), (3, True, False)]
    assert shown["agent"] == [
        (1, True, False),
        (2, False, True),
        (3, False, False),
        (4, False, False),
    ]


def test_suggestions_are_listed_but_not_scored():
    case = make_case()
    style = {"lane": "maintainability", "category": "idiom", "suggestion": True}
    pipeline = [finding(1, 10, posted=True), finding(2, 30, **style)]
    agent = [
        finding(1, 10, verdict="confirmed", posted=True),
        finding(2, 30, verdict="dropped", **style) | {"suggestion": False},
    ]

    text = report(case, pipeline, agent)

    assert "| False positives | 0 (0.00 per PR) | 0 (0.00 per PR) |" in text
    assert "| Suggestions (collapsed, not scored) | 1 | 0 |" in text
    assert "**Scoring rule (applied to all runs):**" in text
    assert "**inline comments only**" in text
    suggestions = text.split("## Suggestions (style, collapsed)")[1].split("##")[0]
    assert "| c | `a.py:30` maintainability/high: finding 2 | false positive | no | dropped" in (
        suggestions
    )


# --- bugs lost to an uncertain verdict ---

LOST_ROW = "| Bugs lost to an uncertain verdict (of which, by the evidence check) | — |"


@pytest.mark.parametrize(
    ("reason", "lost"),
    [
        (
            "confirmed without evidence from code it read (the quote for a.py:10 isn't on that "
            "line): divides by zero",
            "1 (1)",
        ),
        ("could not tell whether values can be empty", "1 (0)"),
    ],
)
def test_bugs_lost_to_uncertain_verdicts_are_counted_by_cause(reason, lost):
    case = make_case()
    pipeline = [finding(1, 10, posted=True)]
    agent = [finding(1, 10, verdict="uncertain", verdict_reason=reason)]

    text = report(case, pipeline, agent)

    assert "| Bugs caught | 1/1 (100%) | 0/1 (0%) |" in text
    assert f"{LOST_ROW} {lost} |" in text  # the pipeline column has no agent: a dash


def test_a_run_with_an_older_prompt_on_todays_code_says_so():
    case = make_case()
    agent = result(case, "agent", []) | {
        "agent_prompt_version": "v1",
        "agent_prompt_override": True,
    }
    results = {(case.id, "pipeline"): result(case, "pipeline", []), (case.id, "agent"): agent}

    assert "**An older prompt on today's code.**" in render("t", [case], results)
    assert "**An older prompt on today's code.**" not in report(case, [], [])


# --- how sure are these numbers: intervals and paired tests ---


def test_wilson_interval_matches_hand_computed_values():
    low, high = wilson(14, 16)
    assert round(low, 3) == 0.640 and round(high, 3) == 0.965
    assert wilson(0, 10)[0] == 0.0  # stays inside 0..1 at 0%
    assert wilson(10, 10)[1] == 1.0  # and at 100%
    assert wilson(0, 0) is None


def test_fisher_one_sided_matches_hand_computed_values():
    # 18 bug comments all kept, 6 of 11 others removed: C(11,6) / C(29,6).
    assert fisher_one_sided(0, 18, 6, 5) == pytest.approx(462 / 475020)
    # Removing one of each from 2 + 2: nothing unusual.
    assert fisher_one_sided(1, 1, 1, 1) == pytest.approx(5 / 6)
    assert fisher_one_sided(0, 3, 0, 3) == 1.0  # nothing removed
    assert fisher_one_sided(0, 0, 0, 0) is None


def test_sign_test_matches_hand_computed_values():
    assert sign_test(2, 0) == 0.5
    assert sign_test(0, 0) == 1.0
    assert sign_test(6, 0) == pytest.approx(0.03125)
    assert sign_test(3, 3) == 1.0


def test_confidence_section_tests_what_the_agent_removed():
    case = make_case()
    pipeline = [finding(i, line, posted=True) for i, line in ((1, 10), (2, 25), (3, 35))]
    agent = [
        finding(1, 10, verdict="confirmed", posted=True),
        finding(2, 25, verdict="dropped"),
        finding(3, 35, verdict="dropped"),
    ]

    text = report(case, pipeline, agent)

    assert "## How sure are these numbers" in text
    assert "| Bugs caught | 1/1: 21–100% | 1/1: 21–100% |" in text
    assert "kept 1 of the 1 that match a planted bug, and 0 of the 2 that match none" in text
    assert "**p = 0.33**" in text  # C(2,2) / C(3,2): removing 2 of 3 at random spares the bug
    assert "This agent against" not in text  # only with --compare


def test_confidence_section_says_when_the_agent_removed_nothing():
    case = make_case()

    text = report(case, [finding(1, 10, posted=True)], [finding(1, 10, posted=True)])

    assert "The agent removed none of the pipeline's inline comments." in text


def test_confidence_section_compares_two_agents_with_a_sign_test():
    text = compared("confirmed", "dropped")  # this agent caught the bug, the baseline didn't

    assert "Bugs caught by only this agent: 1; by only `baseline`: 0. **p = 1**" in text
    assert "False positives posted by only this agent: 0; by only `baseline`: 0." in text
