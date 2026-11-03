"""Format the GitHub review: one inline comment per finding, plus a summary body."""

import re
from collections import Counter

from app.agent.loop import AGENT_PROMPT_VERSION
from app.agent.verify import AgentResult, Check
from app.pipeline.lanes import PROMPT_VERSION, SEVERITIES, Candidate
from app.pipeline.orchestrator import OrchestratorResult

# Evidence locations shown under a confirmed finding.
MAX_EVIDENCE_SHOWN = 3


def safe(text: str) -> str:
    """Neutralize LLM text before our bot posts it. Model output can echo PR content, so:
    - "<!--" is escaped, so it can't plant a fake run marker that would make a later run
      think its review was already posted;
    - "@name" gets a zero-width space, so it can't ping people through our bot."""
    text = text.replace("<!--", "&lt;!--")
    return re.sub(r"@(?=[A-Za-z0-9-])", "@​", text)


def inline_comment(candidate: Candidate, evidence: list[dict] | None = None) -> dict:
    """`evidence` is what the agent checked: only file locations are shown (no free text from
    the agent), and paths come from the repo, so they go through safe() too."""
    body = (
        f"**{candidate.severity}** · {candidate.category} · {safe(candidate.title)}\n\n"
        f"{safe(candidate.explanation)}"
    )
    if candidate.suggested_fix:
        body += f"\n\n**Suggested fix:** {safe(candidate.suggested_fix)}"
    if evidence:
        places = ", ".join(
            f"`{safe(e['path'])}:{e['line']}`" for e in evidence[:MAX_EVIDENCE_SHOWN]
        )
        body += f"\n\n_Checked against {places}_"
    comment = {"path": candidate.file, "line": candidate.line_end, "side": "RIGHT", "body": body}
    if candidate.line_start < candidate.line_end:
        comment["start_line"] = candidate.line_start
        comment["start_side"] = "RIGHT"
    return comment


def summary(
    marker: str,
    head_sha: str,
    models: dict[str, str],
    posted: list[Candidate],
    reviewed_files: list[str],
    skipped_files: dict[str, str],
    lanes: OrchestratorResult,
    agent: AgentResult | None = None,
    suggestions: list[Candidate] = (),
) -> str:
    """`agent` is None when the agent is off (pipeline only): then every finding that passed
    aggregation is shown. `posted` are the inline comments (bug, performance, security);
    `suggestions` are style findings, listed in a collapsed section instead."""
    counts = Counter(c.severity for c in posted)
    issues = "confirmed issue(s)" if agent else "issue(s)"
    if posted:
        found = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
        headline = f"Found {len(posted)} {issues}: {found}."
    else:
        headline = "No confirmed issues found." if agent else "No issues found."
    if suggestions:
        headline += f" {len(suggestions)} style suggestion(s) below."
    lanes_by_model: dict[str, list[str]] = {}
    for lane, model in models.items():
        lanes_by_model.setdefault(model, []).append(lane)
    model_text = " and ".join(
        f"`{model}` ({', '.join(lane_names)})" for model, lane_names in lanes_by_model.items()
    )
    usage = lanes.usage
    lines = [
        marker,
        f"### Automated review of `{head_sha[:7]}`",
        headline,
        f"Reviewed {len(reviewed_files)} file(s) with {model_text}, prompts {PROMPT_VERSION}.",
    ]
    cost = usage.cost_usd
    if agent:
        verdicts = Counter(check.verdict for check in agent.checks)
        lines.append(
            f"Each finding was checked against the repository by the verification agent "
            f"(`{agent.model}`, prompt agent {AGENT_PROMPT_VERSION}): "
            f"{verdicts['confirmed']} confirmed, {verdicts['dropped']} dropped, "
            f"{verdicts['uncertain']} uncertain, {verdicts[None]} not verified. "
            "Only confirmed findings are shown."
        )
        cost += agent.usage.cost_usd
    lines.append(
        f"Cost ≈ ${cost:.4f} at list prices (free tier"
        + (f"; agent ${agent.usage.cost_usd:.4f}" if agent else "")
        + f"); {usage.cached_calls} of {usage.llm_calls + usage.cached_calls} lane call(s) "
        "answered from cache."
    )
    if suggestions:
        # Model text, so through safe() like inline comments.
        items = [
            f"- `{safe(c.file)}:{c.line_start}` {safe(c.title)}: {safe(c.explanation)}"
            for c in suggestions
        ]
        title = f"Suggestions (style, not posted inline): {len(suggestions)}"
        lines += ["", f"<details><summary>{title}</summary>", "", *items, "", "</details>"]
    if agent:
        uncertain = [c for c in agent.checks if c.verdict == "uncertain"]
        not_verified = [c for c in agent.checks if c.verdict is None]
        lines += _collapsed("Uncertain (not posted)", uncertain)
        lines += _collapsed("Not verified (not posted)", not_verified)

    # File names come from the PR, so they go through safe() too.
    not_reviewed = [f"- `{safe(path)}`: {reason}" for path, reason in skipped_files.items()]
    skipped_by_reason: dict[str, list[str]] = {}
    for _, chunk, reason in lanes.skipped_calls:
        skipped_by_reason.setdefault(reason, []).append(chunk.path)
    for reason, paths in skipped_by_reason.items():
        not_reviewed.append(
            f"- {len(paths)} lane call(s) not run ({reason}), in: "
            + ", ".join(f"`{safe(path)}`" for path in sorted(set(paths)))
        )
    if lanes.failed_calls:
        not_reviewed.append(f"- {lanes.failed_calls} lane call(s) failed")
    if not_reviewed:
        lines += ["", "<details><summary>Not fully reviewed</summary>", "", *not_reviewed]
        lines += ["", "</details>"]
    return "\n".join(lines)


def _collapsed(title: str, checks: list[Check]) -> list[str]:
    """A collapsed list of findings that weren't posted, with the reason. The reason can be
    the agent's words (which may echo PR content), so it goes through safe()."""
    if not checks:
        return []
    items = [
        f"- `{safe(c.candidate.file)}:{c.candidate.line_start}` **{c.candidate.severity}** "
        f"{safe(c.candidate.title)} — {safe(c.reason)}"
        for c in checks
    ]
    return ["", f"<details><summary>{title}: {len(checks)}</summary>", "", *items, "", "</details>"]
