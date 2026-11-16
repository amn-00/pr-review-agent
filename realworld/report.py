"""Write realworld/report.md from what was collected and labelled
(docs/plans/real-world-eval.md, section 6).

    uv run python -m realworld.report

It reads only files: prs.json, replays.json, results/*.json, labels.csv, outcomes.json and
reviewer_comments.csv. No database, no GitHub, no LLM. It can be run at any point: comments
without a label yet are counted as such, never guessed.
"""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

from evals.report import _interval, _p, _table, fisher_one_sided
from realworld.collect import LABELS_FILE, RESULTS_DIR
from realworld.common import read_rows
from realworld.outcomes import NEAR_LINES, OUTCOMES_FILE, REVIEWER_FILE
from realworld.replay import read_replays
from realworld.select import PRS_FILE

REPORT_FILE = Path(__file__).parent / "report.md"
LABELS = ("real", "wrong", "nit")

LIMITS = """\
**Read this first.**
- **A small sample:** {sample} real pull requests from {repos} projects. Every rate below
  comes with a 95% interval, and the intervals are wide.
- **Libraries, not applications,** and only PRs that drew a human reviewer's line comment. Such
  PRs may be harder or easier than the average PR.
- **Judged by the person who built the bot.** The labels (`real`, `wrong`, `nit`) are one
  person's reading of the code. Part B doesn't depend on them: it compares the bot with what
  the projects' own reviewers did.
- **No recall.** On real code nobody knows the full list of problems, so this report can't say
  what share of them the bot finds. "Caught what reviewers caught" is the closest it gets, and
  reviewers miss things too.
- **The bot was not changed for this evaluation,** and it had never seen these PRs. They were
  chosen by a fixed rule and a seeded random draw before the first replay (`prs.json`)."""


def yes(value: str) -> bool:
    return value.strip().lower() in ("yes", "y", "true", "1")


def seconds_to_review(result: dict) -> float:
    opened = datetime.fromisoformat(result["pr_created_at"])
    return (datetime.fromisoformat(result["review"]["submitted_at"]) - opened).total_seconds()


def render(
    sample: list[dict],
    replays: dict,
    results: list[dict],
    labels: list[dict],
    outcomes: dict,
    reviewer_rows: list[dict],
) -> str:
    scored = [result for result in results if not result["budget_limited"]]
    scored_ks = {str(result["k"]) for result in scored}
    repos = sorted({entry["repo"] for entry in sample})
    lines = [
        "# The bot on real pull requests",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC from `realworld/`.",
        "",
        LIMITS.format(sample=len(sample), repos=len(repos)),
        "",
        "## Coverage",
        "",
        f"- Replayed: {len(replays)} of {len(sample)} sampled PRs; collected: {len(results)}.",
        f"- Scored: {len(scored)}. Left out as budget-limited (a daily token budget cut the "
        f"review short, or the run didn't complete): {len(results) - len(scored)}.",
    ]
    for result in results:
        if result["budget_limited"]:
            lines.append(f"  - Replay {result['k']}: {'; '.join(result['problems'])}")
    lines += ["", *_precision(scored, labels, repos)]
    lines += _agreement(scored, scored_ks, outcomes, reviewer_rows)
    lines += _production(scored)
    lines += _comments(scored, labels, outcomes)
    return "\n".join(lines)


def _precision(scored: list[dict], labels: list[dict], repos: list[str]) -> list[str]:
    """A: of the bot's inline comments, how many are real problems."""
    label_of = {row["comment_id"]: row["label"].strip().lower() for row in labels}
    rows, unlabelled_total = [], 0
    for name in [*repos, "All"]:
        group = [r for r in scored if name == "All" or r["upstream"]["repo"] == name]
        found = [label_of.get(str(c["id"]), "") for r in group for c in r["comments"]]
        counts = Counter(label if label in LABELS else "unlabelled" for label in found)
        labelled = len(found) - counts["unlabelled"]
        if name == "All":
            unlabelled_total = counts["unlabelled"]
        rows.append(
            [
                name,
                len(group),
                sum(1 for r in group if not r["comments"]),
                len(found),
                counts["real"],
                counts["wrong"],
                counts["nit"],
                _interval(counts["real"], labelled),
                _interval(counts["wrong"], labelled),
            ]
        )
    headers = ["Repo", "PRs", "PRs with no inline comment", "Inline comments", "Real", "Wrong"]
    headers += ["Nit", "Precision (real / labelled)", "Wrong rate"]
    lines = ["## A: are the bot's comments right?", ""]
    if unlabelled_total:
        lines += [f"**{unlabelled_total} comment(s) aren't labelled yet** and aren't counted.", ""]
    lines += _table(headers, rows)
    lines += [
        "",
        "`real`: a genuine problem a maintainer would want to know about. `wrong`: the claim is "
        "false or misreads the code. `nit`: true, but not worth a comment. A PR with no inline "
        "comment isn't a failure: a quiet bot is right on a clean PR.",
        "",
    ]
    return lines


def _agreement(
    scored: list[dict], scored_ks: set[str], outcomes: dict, rows: list[dict]
) -> list[str]:
    """B: the bot against the projects' own reviewers, in both directions."""
    have = [r for r in scored if str(r["k"]) in outcomes]
    lines = ["## B: does the bot agree with the human reviewers?", ""]
    if not have:
        return lines + ["No outcomes yet: run `python -m realworld.outcomes`.", ""]
    totals = Counter()
    for result in have:
        outcome = outcomes[str(result["k"])]
        totals.update({key: outcome[key] for key in ("added", "added_changed", "flagged")})
        totals["flagged_changed"] += outcome["flagged_changed"]
        totals["comments"] += len(outcome["comments"])
        totals["near"] += sum(1 for c in outcome["comments"].values() if c["reviewer_comment_ids"])
    rows = [row for row in rows if row["k"] in scored_ks]
    pairs = [row for row in rows if row["bot_comment_url"]]
    same = {row["bot_comment_url"] for row in pairs if yes(row["same_issue"])}
    unjudged = sum(1 for row in pairs if not row["same_issue"].strip())
    lines += [
        f"**Bot comments near a reviewer's comment** (same file, within {NEAR_LINES} lines, on the "
        f"version both saw): {_interval(totals['near'], totals['comments'])}. Checked by hand "
        f"to be about the same issue: {len(same)}"
        + (f" ({unjudged} pair(s) not judged yet)." if unjudged else "."),
        "",
    ]

    other = totals["added"] - totals["flagged"]
    other_changed = totals["added_changed"] - totals["flagged_changed"]
    # Upper tail: fisher_one_sided gives "this few or fewer of the first group in the first
    # column", so the columns are (unchanged, changed) to ask about "this many or more changed".
    p = fisher_one_sided(
        totals["flagged"] - totals["flagged_changed"],
        totals["flagged_changed"],
        other - other_changed,
        other_changed,
    )
    lines += [
        "**Did the lines the bot flagged change before the merge?** Of the lines these PRs "
        f"added, the bot flagged {totals['flagged']}.",
        "",
        *_table(
            ["Added lines", "Changed or removed before merge"],
            [
                ["Flagged by the bot", _interval(totals["flagged_changed"], totals["flagged"])],
                ["Not flagged (the base rate)", _interval(other_changed, other)],
            ],
        ),
        "",
        f"If the bot's lines were picked at random, the chance of this many or more changing is "
        f"**p = {_p(p)}** (Fisher's exact test, one-sided). Lines in one PR aren't independent, "
        "so read p as a rough guide. A changed line isn't proof the bot was right: the hand "
        "labels in A say that.",
        "",
    ]

    problems = [row for row in rows if yes(row["problem"])]
    unmarked = sum(1 for row in rows if not row["problem"].strip())
    caught = [row for row in problems if row["bot_comment_url"]]
    confirmed = sum(1 for row in caught if yes(row["same_issue"]))
    lines += [
        "**Did the bot catch what the reviewers caught?** The reviewers left "
        f"{len(rows)} line comment(s) on the replayed versions; {len(problems)} are marked as a "
        "real code problem (the rest are style, questions or design talk)"
        + (f"; {unmarked} aren't marked yet" if unmarked else "")
        + f". The bot commented within {NEAR_LINES} lines of "
        f"{_interval(len(caught), len(problems))} of those problems, and {confirmed} of the "
        "matches are confirmed by hand to be the same issue. This isn't recall: reviewers miss "
        "things too.",
        "",
    ]
    return lines


def _production(scored: list[dict]) -> list[str]:
    reviewed = [result for result in scored if result["review"]]
    lines = ["## Production numbers", ""]
    if not reviewed:
        return lines + ["No reviews yet.", ""]
    costs = [float(result["run"]["cost_usd"]) for result in reviewed]
    waits = [seconds_to_review(result) for result in reviewed]
    verdicts = Counter(
        finding["verdict"] or "not verified"
        for result in reviewed
        for finding in result["findings"]
        if not finding["drop_reason"]
    )
    limited = [result for result in reviewed if result["notes"]]
    lines += [
        "These are the deployed bot's own figures (Render free tier, Groq free tier), one PR at "
        "a time.",
        "",
        f"- **Cost per PR at Groq's list prices:** ${mean(costs):.4f} on average, "
        f"${max(costs):.4f} at most.",
        f"- **Time from the PR being opened to the review:** {median(waits) / 60:.1f} minutes "
        f"(median), {max(waits) / 60:.1f} at most. Most of it is waiting for the free tier's "
        "per-minute token limit.",
        "- **The agent's verdicts on the candidates:** "
        + ", ".join(f"{count} {verdict}" for verdict, count in verdicts.most_common())
        + ".",
        f"- **Reviews that hit the per-PR token budget or the time limit:** {len(limited)} of "
        f"{len(reviewed)}. The bot then reviews source files first and lists what it skipped.",
        "",
    ]
    return lines


def _comments(scored: list[dict], labels: list[dict], outcomes: dict) -> list[str]:
    label_of = {row["comment_id"]: row["label"].strip().lower() for row in labels}
    rows = []
    for result in scored:
        per_comment = outcomes.get(str(result["k"]), {}).get("comments", {})
        for comment in result["comments"]:
            outcome = per_comment.get(str(comment["id"]))
            title = comment["body"].splitlines()[0].replace("**", "").replace("|", "\\|")
            rows.append(
                [
                    result["k"],
                    f"[`{comment['file']}:{comment['line']}`]({comment['url']})",
                    title,
                    label_of.get(str(comment["id"])) or "—",
                    ("yes" if outcome["reviewer_comment_ids"] else "no") if outcome else "—",
                    ("yes" if outcome["changed_before_merge"] else "no") if outcome else "—",
                ]
            )
    headers = ["Replay", "Comment", "What it says", "Label", "Near a reviewer's comment"]
    headers += ["Changed before merge"]
    return ["## Every inline comment", "", *_table(headers, rows), ""]


def main() -> None:
    sample = json.loads(PRS_FILE.read_text(encoding="utf-8"))["sample"]
    paths = sorted(RESULTS_DIR.glob("*.json"), key=lambda path: int(path.stem))
    results = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    outcomes = {}
    if OUTCOMES_FILE.exists():
        outcomes = json.loads(OUTCOMES_FILE.read_text(encoding="utf-8"))
    text = render(
        sample, read_replays(), results, read_rows(LABELS_FILE), outcomes, read_rows(REVIEWER_FILE)
    )
    REPORT_FILE.write_text(text + "\n", encoding="utf-8")
    print(f"Wrote {REPORT_FILE}")


if __name__ == "__main__":
    main()
