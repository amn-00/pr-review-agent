"""Scoring and the known-issue analyses in the eval report, on hand-built results."""

from evals.dataset import Case, Label
from evals.report import calls_cost, corroborated, render, score, truth

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
