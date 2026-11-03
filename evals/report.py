"""Score the saved eval results against the case labels, and write report.md next to them.

    uv run python -m evals.report --name baseline
    uv run python -m evals.report --name agent-v2 --compare baseline
    uv run python -m evals.report --name heldout --cases heldout

Uses only the result files and the cases: no database, no LLM. Labels can be edited and the
report rebuilt without running anything again.
"""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from app.agent.loop import CALLED_AS, MAX_AGENT_STEPS, NO_EVIDENCE, VERDICT_JSON
from app.agent.tools import VERDICT_TOOL
from app.llm.pricing import cost_usd
from app.pipeline.lanes import SUGGESTION_CATEGORIES
from evals.dataset import Case, Label, load_cases

RESULTS_DIR = Path(__file__).resolve().parent / "results"
MODES = ("pipeline", "agent", "repush")

LIMITS = """\
**Read this first.**
- This is a synthetic benchmark of {cases} small Python cases, written for this project. The
  bugs were planted by the same people who built the reviewer, so it shows how the parts
  behave relative to each other, not how well the reviewer does on real-world PRs.
- One sample per case and mode. The lane cache makes both modes judge exactly the same
  candidates, but the LLM's run-to-run variance is not measured: a rerun could differ.
- With this few cases, a difference of 1–2 findings is not meaningful.
- Latency is on Groq's free tier, where most of it is waiting for the per-minute token limit."""
TUNED = """\
- **Tuned on the test set.** Every agent prompt after v1 was written after reading earlier
  results on these same cases, so its scores here are optimistic. Only cases it was never
  tuned on (`evals/heldout/`) show whether it generalizes."""
HELD_OUT = """\
- **Held-out cases.** These were written after the last agent prompt and never used to tune
  it. They're a check that the tuning generalizes, not a measurement: there are only a few, so
  a single finding moves every rate a lot."""
PROMPT_OVERRIDE = """\
- **An older prompt on today's code.** At least one agent here ran an older prompt version
  (`--agent-prompt`) with today's agent code: the evidence quote check and the handling of
  misnamed verdicts apply to it too. Its column compares prompts, not the agent as it was."""


@dataclass
class Score:
    """One case in one mode, judged on what was posted."""

    caught: list[tuple[Label, dict]]  # each expected bug caught, with a finding that caught it
    missed: list[Label]
    true_positives: int  # posted findings that match an expected bug
    false_positives: list[dict]  # posted findings matching no expected or acceptable label
    traps_posted: list[Label]


def score(case: Case, findings: list[dict]) -> Score:
    posted = [f for f in findings if f["posted"]]
    caught, missed = [], []
    for label in case.expect:
        match = next((f for f in posted if label.matches(f)), None)
        if match:
            caught.append((label, match))
        else:
            missed.append(label)
    return Score(
        caught=caught,
        missed=missed,
        true_positives=sum(1 for f in posted if _matches_any(f, case.expect)),
        false_positives=[f for f in posted if not _matches_any(f, case.expect + case.acceptable)],
        traps_posted=[label for label in case.traps if any(label.matches(f) for f in posted)],
    )


def truth(case: Case, finding: dict) -> str:
    """What a finding is, by the labels: "bug", "acceptable", "trap" or "false positive"."""
    if _matches_any(finding, case.expect):
        return "bug"
    if _matches_any(finding, case.acceptable):
        return "acceptable"
    return "trap" if _matches_any(finding, case.traps) else "false positive"


def calls_cost(result: dict, agent: bool) -> float:
    """List-price cost of a result's lane calls (agent=False) or agent calls, including the
    calls of earlier incomplete attempts."""
    calls = result["earlier_calls"] + result["calls"]
    return float(
        sum(
            cost_usd(c["model"], c["tokens_in"], c["tokens_out"])
            for c in calls
            if (c["kind"] == "agent") == agent
        )
    )


def corroborated(finding: dict, tool_findings: list[dict]) -> bool:
    """A Ruff or Semgrep finding on one of its lines, given to the same lane as a hint."""
    return any(
        t["file"] == finding["file"]
        and t["lane"] == finding["lane"]
        and finding["line_start"] <= t["line"] <= finding["line_end"]
        for t in tool_findings
    )


def load_results(results_dir: Path) -> dict[tuple[str, str], dict]:
    results = {}
    for path in sorted(results_dir.glob("*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        apply_posting_rule(result)
        results[(result["case"], result["mode"])] = result
    return results


def apply_posting_rule(result: dict) -> None:
    """Score every run by the current rule, including runs made before it existed, so they can
    be compared: a finding is shown if it passed aggregation and, with the agent, was confirmed.
    Shown style findings are suggestions (`suggestion`); the rest are inline comments
    (`posted`, the only ones scored)."""
    with_agent = result["mode"] == "agent"
    for finding in result["findings"]:
        shown = not finding["drop_reason"] and (not with_agent or finding["verdict"] == "confirmed")
        finding["suggestion"] = shown and finding["category"] in SUGGESTION_CATEGORIES
        finding["posted"] = shown and not finding["suggestion"]


def current(results: dict, case: Case, mode: str) -> dict | None:
    """The result for this case and mode if it's complete and for the case's current files."""
    result = results.get((case.id, mode))
    if result and result["status"] == "complete" and result["head_sha"] == case.target(mode)[1]:
        return result
    return None


def render(
    name: str,
    cases: list[Case],
    results: dict,
    compare: tuple[str, dict] | None = None,
    held_out: bool = False,
) -> str:
    """`compare` is (name, results) of another eval with the same pipeline results, e.g. the
    baseline, to put its agent next to this one. `held_out`: the cases come from
    evals/heldout/, which no prompt was tuned on."""
    paired = [
        (case, current(results, case, "pipeline"), current(results, case, "agent"))
        for case in cases
        if current(results, case, "pipeline") and current(results, case, "agent")
    ]
    if compare:
        other_name, other_results = compare
        other = {case.id: current(other_results, case, "agent") for case, _, _ in paired}
        paired = [(case, p, a) for case, p, a in paired if other[case.id]]
        compare = (other_name, other)
    limits = LIMITS.format(cases=len(cases))
    if held_out:
        limits += "\n" + HELD_OUT
    elif any((agent.get("agent_prompt_version") or "v1") != "v1" for _, _, agent in paired):
        limits += "\n" + TUNED
    others = [agent for agent in compare[1].values() if agent] if compare else []
    agents = [agent for _, _, agent in paired] + others
    if any(agent.get("agent_prompt_override") for agent in agents):
        limits += "\n" + PROMPT_OVERRIDE
    lines = [
        f"# Eval report: {name}",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from `evals/results/{name}/`.",
        "",
        limits,
        "",
    ]
    lines += _summary(paired, len(cases), compare)
    if compare:
        lines += _changed_verdicts(paired, compare)
    lines += _per_case(paired)
    lines += _funnel(paired)
    lines += _matches(paired)
    lines += _false_positives(paired)
    lines += _suggestions(paired)
    lines += _injection(paired)
    lines += _dedupe(paired)
    lines += _corroboration(paired)
    lines += _agent(paired)
    lines += _repush(cases, results)
    lines += _runs(cases, results)
    lines += _labels_added_after_run(cases)
    return "\n".join(lines)


@dataclass
class Totals:
    """One column of the summary: every paired case in one mode."""

    caught: int
    expected: int
    true_positives: int
    false_positives: int
    clean_with_fp: int
    clean: int
    traps_posted: int
    traps: int
    # Planted bugs missed because the agent ended "uncertain" on a finding that reported them,
    # and how many of those the evidence check made uncertain (a missing or wrong quote, or a
    # line the agent wasn't shown).
    lost_to_uncertain: int
    lost_to_evidence_check: int


def totals(runs: list[tuple[Case, dict]], mode: str) -> Totals:
    """`runs` holds (case, {"pipeline": ..., "agent": ...}); `mode` picks which one counts."""
    scores = [(case, score(case, r[mode]["findings"])) for case, r in runs]
    uncertain = [
        [
            f
            for f in r[mode]["findings"]
            if not f["drop_reason"] and f["verdict"] == "uncertain" and label.matches(f)
        ]
        for (case, r), (_, s) in zip(runs, scores, strict=True)
        for label in s.missed
    ]
    return Totals(
        lost_to_uncertain=sum(1 for found in uncertain if found),
        lost_to_evidence_check=sum(
            1
            for found in uncertain
            if any(NO_EVIDENCE in (f["verdict_reason"] or "") for f in found)
        ),
        caught=sum(len(s.caught) for _, s in scores),
        expected=sum(len(case.expect) for case, _ in scores),
        true_positives=sum(s.true_positives for _, s in scores),
        false_positives=sum(len(s.false_positives) for _, s in scores),
        clean_with_fp=sum(1 for case, s in scores if case.kind == "clean" and s.false_positives),
        clean=sum(1 for case, _ in scores if case.kind == "clean"),
        traps_posted=sum(len(s.traps_posted) for _, s in scores),
        traps=sum(len(case.traps) for case, _ in scores),
    )


SUMMARY_METRICS = [
    "Bugs caught",
    # Does the evidence check (quotes) cost real bugs?
    "Bugs lost to an uncertain verdict (of which, by the evidence check)",
    # Next to the catch rate: catching more is only a win if these don't come back.
    "Traps posted",
    "Clean PRs with a false positive",
    "False positives",
    "Inline comments",
    "Precision (inline comments)",
    "Suggestions (collapsed, not scored)",
    "Cost per PR (list prices)",
    "Latency per PR",
    "Rate-limit waits per PR (summed over parallel calls)",
]


def _column(runs: list[tuple[Case, dict]], mode: str) -> list[str]:
    t = totals(runs, mode)
    costs = [_cost(r, mode) for _, r in runs]
    latencies = [_latency(r, mode) for _, r in runs if _latency(r, mode) is not None]
    waits = [_waits(r, mode) for _, r in runs]
    posted = t.true_positives + t.false_positives
    suggestions = sum(1 for _, r in runs for f in r[mode]["findings"] if f.get("suggestion"))
    lost = f"{t.lost_to_uncertain} ({t.lost_to_evidence_check})" if mode == "agent" else "—"
    return [
        f"{t.caught}/{t.expected} ({_pct(t.caught, t.expected)})",
        lost,
        f"{t.traps_posted}/{t.traps}",
        f"{t.clean_with_fp}/{t.clean}",
        f"{t.false_positives} ({t.false_positives / len(runs):.2f} per PR)",
        str(posted),
        _pct(t.true_positives, posted),
        str(suggestions),
        f"${mean(costs):.4f} (max ${max(costs):.4f})",
        f"{mean(latencies):.0f} s (max {max(latencies):.0f} s)" if latencies else "n/a",
        f"{mean(waits):.0f} s",
    ]


SCORING_RULE = (
    "**Scoring rule (applied to all runs):** precision, false positives and traps count "
    "**inline comments only**: findings in the categories bug, performance and security that "
    "passed aggregation and, with the agent, were confirmed. Style findings (readability, idiom) "
    "are collapsed suggestions: counted and listed separately, never scored. Every run is scored "
    "this way, including runs made before the rule existed, so numbers in older reports differ."
)


def _summary(paired: list, total_cases: int, compare: tuple[str, dict] | None) -> list[str]:
    lines = ["## Pipeline only vs pipeline + agent", "", SCORING_RULE, ""]
    if not paired:
        return lines + ["No case has complete results in both modes yet.", ""]
    runs = [(case, {"pipeline": pipeline, "agent": agent}) for case, pipeline, agent in paired]
    headers = ["Metric", "Pipeline only", f"Pipeline + agent ({_versions(paired)})"]
    columns = [_column(runs, "pipeline"), _column(runs, "agent")]
    if compare:
        other_name, other = compare
        # The other eval's agent on the same candidates (this eval's pipeline results are
        # copies of the other's).
        other_runs = [(case, r | {"agent": other[case.id]}) for case, r in runs]
        other_paired = [(case, r["pipeline"], r["agent"]) for case, r in other_runs]
        headers.insert(2, f"Pipeline + agent ({other_name}, {_versions(other_paired)})")
        columns.insert(1, _column(other_runs, "agent"))
        lines += [f"{len(paired)} of {total_cases} cases have complete results in all three.", ""]
    else:
        lines += [f"{len(paired)} of {total_cases} cases have complete results in both modes.", ""]
    table = [[m, *(column[i] for column in columns)] for i, m in enumerate(SUMMARY_METRICS)]
    lines += _table(headers, table)
    if compare:
        lines += ["", *_over_correction(runs, other_runs, compare[0])]
    lines += [
        "",
        "Pipeline + agent cost and latency = the pipeline run's plus the agent's (the agent run "
        "got every lane call from the cache). Latency leaves out runs that took more than one "
        "attempt. False positives are inline comments that match no planted bug and no "
        "`acceptable` label; precision = inline comments matching a planted bug / all inline "
        "comments that aren't `acceptable`. A bug lost to an uncertain verdict was reported by a "
        "finding that passed aggregation but that the agent ended `uncertain`; the number in "
        "brackets is how many of those the evidence check made uncertain (a quote not on its "
        "line, or a line the agent wasn't shown).",
        "",
    ]
    return lines


def _over_correction(runs: list, other_runs: list, other_name: str) -> list[str]:
    """Did catching more bugs bring false positives back? The agent wins only if it didn't."""
    new, old = totals(runs, "agent"), totals(other_runs, "agent")
    deltas = {
        "bugs caught": new.caught - old.caught,
        "traps posted": new.traps_posted - old.traps_posted,
        "clean PRs with a false positive": new.clean_with_fp - old.clean_with_fp,
        "false positives": new.false_positives - old.false_positives,
    }
    wins = deltas["bugs caught"] > 0 and all(
        change <= 0 for name, change in deltas.items() if name != "bugs caught"
    )
    changes = ", ".join(f"{name} {change:+d}" for name, change in deltas.items())
    return [
        f"**Over-correction check, against `{other_name}`:** {changes}. This agent wins only "
        "if bugs caught go up and none of the other three do: "
        + ("**passes**." if wins else "**doesn't pass**.")
        + " With one sample per finding, a change of 1–2 can be noise.",
    ]


def _changed_verdicts(paired: list, compare: tuple[str, dict]) -> list[str]:
    """Findings the two agents judged differently. The candidates are the same (one lane
    cache), so each is matched to its twin in the other run."""
    other_name, other = compare
    rows = []
    for case, _, agent in paired:
        for finding in agent["findings"]:
            if finding["drop_reason"]:
                continue
            twin = _twin(finding, other[case.id]["findings"])
            before = twin["verdict"] if twin else None
            if before == finding["verdict"]:
                continue
            reason = (finding["verdict_reason"] or "")[:200]
            rows.append(
                [
                    case.id,
                    _describe(finding),
                    truth(case, finding),
                    before or "—",
                    f"{finding['verdict'] or '—'}: {reason}",
                ]
            )
    headers = ["Case", "Finding", "Label", f"Verdict ({other_name})", "Verdict (this run)"]
    return ["## Changed verdicts", "", *_table(headers, rows), ""]


def _versions(paired: list) -> str:
    """The agent prompt version(s) of these agent results; results from before the field
    existed ran agent v1."""
    versions = sorted({agent.get("agent_prompt_version") or "v1" for _, _, agent in paired})
    return "agent " + ", ".join(versions)


def _per_case(paired: list) -> list[str]:
    rows = []
    for case, pipeline, agent in paired:
        p, a = score(case, pipeline["findings"]), score(case, agent["findings"])
        both = {"pipeline": pipeline, "agent": agent}
        notes = "; ".join(pipeline["notes"] + agent["notes"])
        rows.append(
            [
                case.id,
                case.kind,
                f"{len(p.caught)} / {len(a.caught)} of {len(case.expect)}",
                f"{len(p.false_positives)} / {len(a.false_positives)}",
                f"${_cost(both, 'pipeline'):.4f} / ${_cost(both, 'agent'):.4f}",
                f"{pipeline['wall_seconds']:.0f} / {agent['wall_seconds']:.0f} s",
                notes,
            ]
        )
    headers = [
        "Case",
        "Kind",
        "Caught (P / A)",
        "FP (P / A)",
        "Cost (P / A)",
        "Wall (P run / A run)",
    ]
    return ["## Per case", "", *_table(headers + ["Notes"], rows), ""]


def _funnel(paired: list) -> list[str]:
    rows = []
    for case, pipeline, agent in paired:
        for label in case.expect:
            found = [f for f in pipeline["findings"] if label.matches(f)]
            rows.append(
                [
                    case.id,
                    label.note,
                    _yes(found),
                    _yes(f for f in found if not f["drop_reason"]),
                    _yes(f for f in pipeline["findings"] if f["posted"] and label.matches(f)),
                    _yes(f for f in agent["findings"] if f["posted"] and label.matches(f)),
                ]
            )
    headers = [
        "Case",
        "Bug",
        "A lane found it",
        "Survived aggregate",
        "Posted (P)",
        "Confirmed (A)",
    ]
    return ["## Where each bug was lost", "", *_table(headers, rows), ""]


def _matches(paired: list) -> list[str]:
    rows = []
    for case, pipeline, agent in paired:
        for mode, result in (("P", pipeline), ("A", agent)):
            for label, finding in score(case, result["findings"]).caught:
                rows.append([case.id, label.note, mode, _describe(finding)])
    intro = "Every catch, so the automatic matching can be checked by eye."
    return ["## Matches", "", intro, "", *_table(["Case", "Bug", "Mode", "Finding"], rows), ""]


def _false_positives(paired: list) -> list[str]:
    rows = []
    for case, pipeline, agent in paired:
        for finding in score(case, pipeline["findings"]).false_positives:
            twin = _twin(finding, agent["findings"])
            verdict = f"{twin['verdict']}: {twin['verdict_reason']}" if twin else "?"
            rows.append([case.id, _describe(finding), truth(case, finding), verdict])
    intro = (
        "Posted by the pipeline and matching no label, with what the agent decided. A real "
        "issue here that wasn't planted can be added as `[[acceptable]]` "
        "(with `added_after_run = true`)."
    )
    headers = ["Case", "Finding", "Label", "Agent verdict"]
    return ["## False positives", "", intro, "", *_table(headers, rows), ""]


def _suggestions(paired: list) -> list[str]:
    """Style findings shown in the collapsed section: every one the pipeline would show, and
    whether the agent kept it. They aren't scored, but a wrong suggestion is still noise."""
    rows = []
    for case, pipeline, agent in paired:
        for finding in pipeline["findings"]:
            if not finding.get("suggestion"):
                continue
            twin = _twin(finding, agent["findings"])
            verdict = f"{twin['verdict']}: {twin['verdict_reason']}" if twin else "?"
            shown = "yes" if twin and twin.get("suggestion") else "no"
            rows.append([case.id, _describe(finding), truth(case, finding), shown, verdict])
    headers = ["Case", "Suggestion", "Label", "Shown with the agent", "Agent verdict"]
    return ["## Suggestions (style, collapsed)", "", *_table(headers, rows), ""]


def _injection(paired: list) -> list[str]:
    rows = []
    for case, pipeline, agent in paired:
        if case.kind != "injection":
            continue
        for label in case.expect:
            candidate = next(
                (f for f in agent["findings"] if not f["drop_reason"] and label.matches(f)), None
            )
            tools = (
                ", ".join(
                    f"{s['tool']}({json.dumps(s['args_json'])})" for s in candidate["agent_steps"]
                )
                if candidate
                else ""
            )
            rows.append(
                [
                    case.id,
                    label.note,
                    _yes(f for f in pipeline["findings"] if f["posted"] and label.matches(f)),
                    f"{candidate['verdict']}: {candidate['verdict_reason']}" if candidate else "—",
                    tools,
                ]
            )
    headers = ["Case", "Bug", "Posted (P)", "Agent verdict", "Agent's tool calls"]
    return ["## Prompt injection", "", *_table(headers, rows), ""]


def _dedupe(paired: list) -> list[str]:
    lines = ["## Known issue: cross-lane dedupe", ""]
    duplicates, cross_lane, rows = 0, 0, []
    for case, pipeline, _ in paired:
        by_id = {f["id"]: f for f in pipeline["findings"]}
        caught = score(case, pipeline["findings"]).caught
        for finding in pipeline["findings"]:
            if finding["drop_reason"] != "duplicate":
                continue
            winner = by_id[finding["duplicate_of"]]
            duplicates += 1
            cross_lane += winner["lane"] != finding["lane"]
            for label in case.expect:
                if label.matches(finding) and not label.matches(winner):
                    still = any(lab is label for lab, _ in caught)
                    rows.append(
                        [case.id, label.note, _describe(finding), _describe(winner), _yesno(still)]
                    )
    lines += [
        f"{duplicates} duplicate(s) dropped by aggregate, {cross_lane} of them across lanes.",
        "",
        "Duplicates that were a planted bug their winner isn't:",
        "",
    ]
    headers = ["Case", "Bug", "Duplicate", "Kept instead", "Bug still caught"]
    lines += _table(headers, rows) + [""]

    lines += ["## Known issue: dedupe before verification", ""]
    rows = []
    for case, _, agent in paired:
        by_id = {f["id"]: f for f in agent["findings"]}
        for finding in agent["findings"]:
            if finding["drop_reason"] != "duplicate":
                continue
            winner = by_id[finding["duplicate_of"]]
            if winner["verdict"] != "confirmed":
                rows.append(
                    [case.id, _describe(finding), truth(case, finding), winner["verdict"] or "—"]
                )
    lines += [
        f"{len(rows)} duplicate(s) whose winner the agent didn't confirm, so they were never "
        "checked or posted.",
        "",
    ]
    headers = ["Case", "Duplicate", "Label", "Winner's verdict"]
    return lines + _table(headers, rows) + [""]


def _corroboration(paired: list) -> list[str]:
    counts: Counter = Counter()
    tokens, rows = 0, []
    for case, _, agent in paired:
        for finding in agent["findings"]:
            if finding["drop_reason"] or not corroborated(finding, agent["tool_findings"]):
                continue
            kind, verdict = truth(case, finding), finding["verdict"] or "not verified"
            counts[(kind, verdict)] += 1
            tokens += sum(s["tokens_in"] + s["tokens_out"] for s in finding["agent_steps"])
            if kind != "bug":
                rows.append([case.id, _describe(finding), kind, verdict])
    lines = [
        "## Known issue: skip the agent for Ruff/Semgrep-corroborated findings?",
        "",
        f"{sum(counts.values())} finding(s) had a tool finding on their lines; the agent spent "
        f"{tokens} tokens on them (what a skip would save).",
        "",
    ]
    table = [[kind, verdict, str(n)] for (kind, verdict), n in sorted(counts.items())]
    lines += _table(["Label", "Agent verdict", "Findings"], table)
    lines += ["", "Corroborated findings that aren't planted bugs (a skip would post these):", ""]
    return lines + _table(["Case", "Finding", "Label", "Agent verdict"], rows) + [""]


def _agent(paired: list) -> list[str]:
    checked = [f for _, _, agent in paired for f in agent["findings"] if not f["drop_reason"]]
    verdicts = Counter(f["verdict"] or "not verified" for f in checked)
    with_steps = [f for f in checked if f["agent_steps"]]
    steps = [len(f["agent_steps"]) for f in with_steps]
    tokens = [sum(s["tokens_in"] + s["tokens_out"] for s in f["agent_steps"]) for f in with_steps]
    reasons = Counter(f["verdict_reason"] for f in checked if f["verdict"] in (None, "uncertain"))
    # Past MAX_AGENT_STEPS: the forced verdict didn't come, so it was retried, then asked for
    # as JSON (app/agent/loop.py).
    retried = sum(1 for n in steps if n > MAX_AGENT_STEPS)
    json_fallback = sum(
        1 for f in with_steps if any(s["tool"] == VERDICT_JSON for s in f["agent_steps"])
    )
    failed_calls = sum(1 for f in with_steps for s in f["agent_steps"] if s["tool"] is None)
    misnamed = sum(
        1
        for f in with_steps
        for s in f["agent_steps"]
        if s["tool"] == VERDICT_TOOL and s["result_summary"].startswith(CALLED_AS)
    )
    lines = ["## Verification agent", ""]
    if not with_steps:
        return lines + ["No agent steps yet.", ""]
    lines += [
        "Verdicts: " + ", ".join(f"{v} {n}" for v, n in sorted(verdicts.items())) + ".",
        f"Steps per finding: mean {mean(steps):.1f}, max {max(steps)}; "
        f"{sum(1 for n in steps if n >= MAX_AGENT_STEPS)} reached the {MAX_AGENT_STEPS}-step "
        f"limit. The forced verdict had to be retried for {retried}, and asked for as JSON "
        f"for {json_fallback}.",
        f"Steps that failed or called no tool (e.g. rejected by Groq): {failed_calls} of "
        f"{sum(steps)}. Verdicts sent under a wrong tool name and used: {misnamed}.",
        f"Tokens per finding: mean {mean(tokens):.0f}, max {max(tokens)}.",
        "",
        "Why findings ended uncertain or not verified:",
        "",
    ]
    return lines + _table(["Reason", "Findings"], [[r, str(n)] for r, n in reasons.items()]) + [""]


def _repush(cases: list[Case], results: dict) -> list[str]:
    rows = []
    for case in cases:
        result = current(results, case, "repush")
        if not result:
            continue
        run = result["run"]
        spent = run["tokens_in"] + run["tokens_out"]
        rows.append(
            [
                case.id,
                f"{run['cached_calls']} of {run['cached_calls'] + run['llm_calls']}",
                str(run["tokens_saved"]),
                str(spent),
                _pct(run["tokens_saved"], run["tokens_saved"] + spent),
            ]
        )
    headers = ["Case", "Lane calls from cache", "Tokens saved", "Tokens spent", "Saved"]
    intro = "A second push that changes one file; pipeline mode."
    return ["## Cache on a re-push", "", intro, "", *_table(headers, rows), ""]


def _runs(cases: list[Case], results: dict) -> list[str]:
    rows = []
    for case in cases:
        for mode in MODES:
            if mode == "repush" and case.push2 is None:
                continue
            result = results.get((case.id, mode))
            if result is None:
                status = "not run"
            elif result["head_sha"] != case.target(mode)[1]:
                status = "stale (case changed since)"
            else:
                status = result["status"]
            details = "; ".join(
                (result or {}).get("problems", []) + (result or {}).get("notes", [])
            )
            rows.append([case.id, mode, status, details])
    return ["## Runs", "", *_table(["Case", "Mode", "Status", "Details"], rows), ""]


def _labels_added_after_run(cases: list[Case]) -> list[str]:
    rows = [
        [case.id, f"{label.file}:{label.lines[0]}-{label.lines[1]}", label.note]
        for case in cases
        for label in case.acceptable + case.expect + case.traps
        if label.added_after_run
    ]
    intro = "Labels added after seeing model output (they change the scores above)."
    return [
        "## Labels added after the run",
        "",
        intro,
        "",
        *_table(["Case", "Lines", "Note"], rows),
    ]


def _cost(results: dict, mode: str) -> float:
    """Pipeline only: the lanes. Pipeline + agent: the lanes plus the agent run's agent calls.
    `results` is {"pipeline": ..., "agent": ...}."""
    cost = calls_cost(results["pipeline"], agent=False)
    if mode == "agent":
        cost += calls_cost(results["agent"], agent=True)
    return cost


def _latency(results: dict, mode: str) -> float | None:
    used = [results["pipeline"]] + ([results["agent"]] if mode == "agent" else [])
    if any(r["earlier_calls"] for r in used):
        return None  # spread over attempts: not comparable
    return sum(r["wall_seconds"] for r in used)


def _waits(results: dict, mode: str) -> float:
    used = [results["pipeline"]] + ([results["agent"]] if mode == "agent" else [])
    return sum(c["waited_s"] for r in used for c in r["calls"])


def _twin(finding: dict, findings: list[dict]) -> dict | None:
    """The same candidate in the other mode's run (the lanes came from the same cache)."""
    key = ("lane", "file", "line_start", "line_end", "title")
    return next((f for f in findings if all(f[k] == finding[k] for k in key)), None)


def _matches_any(finding: dict, labels: list[Label]) -> bool:
    return any(label.matches(finding) for label in labels)


def _describe(finding: dict) -> str:
    lines = f"{finding['line_start']}" + (
        f"-{finding['line_end']}" if finding["line_end"] != finding["line_start"] else ""
    )
    title = finding["title"].replace("|", "\\|")
    return f"`{finding['file']}:{lines}` {finding['lane']}/{finding['severity']}: {title}"


def _yes(findings) -> str:
    return _yesno(any(True for _ in findings))


def _yesno(value: bool) -> str:
    return "yes" if value else "**no**"


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["(none)"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="results folder under evals/results/")
    parser.add_argument(
        "--compare",
        metavar="NAME",
        help="another eval with the same pipeline results, whose agent is shown alongside",
    )
    parser.add_argument(
        "--cases",
        default="cases",
        metavar="FOLDER",
        help="case folder under evals/: cases (default, the tuning set) or heldout",
    )
    args = parser.parse_args()
    results_dir = RESULTS_DIR / args.name
    compare = (args.compare, load_results(RESULTS_DIR / args.compare)) if args.compare else None
    cases = load_cases(Path(__file__).resolve().parent / args.cases)
    results = load_results(results_dir)
    text = render(args.name, cases, results, compare, held_out=args.cases == "heldout")
    (results_dir / "report.md").write_text(text + "\n", encoding="utf-8")
    print(f"Wrote {results_dir / 'report.md'}")


if __name__ == "__main__":
    main()
