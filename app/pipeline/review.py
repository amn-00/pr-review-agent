"""Format the GitHub review: one inline comment per finding, plus a summary body."""

import re
from collections import Counter

from app.pipeline.lanes import PROMPT_VERSION, SEVERITIES, Candidate
from app.pipeline.orchestrator import OrchestratorResult


def safe(text: str) -> str:
    """Neutralize LLM text before our bot posts it. Model output can echo PR content, so:
    - "<!--" is escaped, so it can't plant a fake run marker that would make a later run
      think its review was already posted;
    - "@name" gets a zero-width space, so it can't ping people through our bot."""
    text = text.replace("<!--", "&lt;!--")
    return re.sub(r"@(?=[A-Za-z0-9-])", "@​", text)


def inline_comment(candidate: Candidate) -> dict:
    body = (
        f"**{candidate.severity}** · {candidate.category} · {safe(candidate.title)}\n\n"
        f"{safe(candidate.explanation)}"
    )
    if candidate.suggested_fix:
        body += f"\n\n**Suggested fix:** {safe(candidate.suggested_fix)}"
    comment = {"path": candidate.file, "line": candidate.line_end, "side": "RIGHT", "body": body}
    if candidate.line_start < candidate.line_end:
        comment["start_line"] = candidate.line_start
        comment["start_side"] = "RIGHT"
    return comment


def summary(
    marker: str,
    head_sha: str,
    model: str,
    posted: list[Candidate],
    reviewed_files: list[str],
    skipped_files: dict[str, str],
    lanes: OrchestratorResult,
) -> str:
    counts = Counter(c.severity for c in posted)
    if posted:
        found = ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
        headline = f"Found {len(posted)} issue(s): {found}."
    else:
        headline = "No issues found."
    lines = [
        marker,
        f"### Automated review of `{head_sha[:7]}`",
        headline,
        f"Reviewed {len(reviewed_files)} file(s) with `{model}` (prompts {PROMPT_VERSION}).",
    ]

    # File names come from the PR, so they go through safe() too.
    not_reviewed = [f"- `{safe(path)}`: {reason}" for path, reason in skipped_files.items()]
    if lanes.skipped_calls:
        files = sorted({chunk.path for _, chunk in lanes.skipped_calls})
        not_reviewed.append(
            f"- {len(lanes.skipped_calls)} lane call(s) not run (time limit), in: "
            + ", ".join(f"`{safe(path)}`" for path in files)
        )
    if lanes.failed_calls:
        not_reviewed.append(f"- {lanes.failed_calls} lane call(s) failed")
    if not_reviewed:
        lines += ["", "<details><summary>Not fully reviewed</summary>", "", *not_reviewed]
        lines += ["", "</details>"]
    return "\n".join(lines)
