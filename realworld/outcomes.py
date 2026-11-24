"""B: did the bot's comments land where the human reviewers also saw a problem, and did it
catch what they caught? (docs/plans/real-world-eval.md, section 5)

    uv run python -m realworld.outcomes

For every collected PR (realworld/results/<k>.json), from the upstream PR's public history:
- which lines the replayed version added, and which of them changed before the merge;
- the reviewers' line comments made on the replayed version, and which sit within 3 lines of a
  bot comment.
It writes realworld/outcomes.json, and adds each reviewer comment to
realworld/reviewer_comments.csv, where the owner marks `problem` and `same_issue` by hand.
It reads public GitHub data only, and is safe to run again.
"""

import json
import re
from pathlib import Path

import httpx

from realworld.collect import RESULTS_DIR
from realworld.common import get, gh_token, github_client, iter_items, merge_rows
from realworld.replay import GITHUB_URL, WORK_DIR, fetch_upstream_pull, git
from realworld.select import is_person

OUTCOMES_FILE = Path(__file__).parent / "outcomes.json"
REVIEWER_FILE = Path(__file__).parent / "reviewer_comments.csv"
REVIEWER_COLUMNS = ["k", "comment_id", "url", "file", "line", "text", "problem"] + [
    "bot_comment_url",
    "same_issue",
]
# A bot comment and a reviewer's comment "match" when they're this close in the same file.
NEAR_LINES = 3
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_lines(work: Path, old: str, new: str, side: str) -> dict[str, set[int]]:
    """Line numbers a diff touches, per file, from `git diff` without context lines.

    side "old": the lines of `old` that `new` changed or removed, under their path in `old`
    (so a renamed file is found by its old name, and lines that only moved don't count).
    side "new": the lines `new` added or changed, under their path in `new`.
    """
    text = git(work, "-c", "core.quotepath=false", "diff", "-U0", "-M", old, new)
    marker, group = ("--- a/", 1) if side == "old" else ("+++ b/", 3)
    lines: dict[str, set[int]] = {}
    path, in_header = None, False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and line.startswith(marker):
            path = line[len(marker) :]  # stays None for "/dev/null": no such file on this side
        elif match := HUNK.match(line):
            # Past the file's header: a changed line that starts with "--- " is content now.
            in_header = False
            start, count = int(match.group(group)), int(match.group(group + 1) or 1)
            if path and count:  # count 0: nothing on this side, e.g. a pure insertion
                lines.setdefault(path, set()).update(range(start, start + count))
    return lines


def reviewer_comments(comments: list[dict], author: str, replay_sha: str) -> list[dict]:
    """The reviewers' own line comments on the replayed version: by a person other than the
    PR's author, made on the replayed commit (so their line numbers are the ones the bot saw),
    on the new side of the diff, and not a reply inside a thread."""
    return [
        {
            "id": comment["id"],
            "url": comment["html_url"],
            "file": comment["path"],
            "line": comment["original_line"],
            "start_line": comment["original_start_line"] or comment["original_line"],
            "text": " ".join(comment["body"].split())[:300],
        }
        for comment in comments
        if is_person(comment["user"])
        and comment["user"]["login"] != author
        and comment["original_commit_id"] == replay_sha
        and comment.get("in_reply_to_id") is None
        and comment.get("side", "RIGHT") == "RIGHT"
        and comment["original_line"] is not None
    ]


def near(a: dict, b: dict) -> bool:
    """Two comments in the same file whose line ranges are within NEAR_LINES of each other."""
    return (
        a["file"] == b["file"]
        and a["start_line"] - NEAR_LINES <= b["line"]
        and b["start_line"] <= a["line"] + NEAR_LINES
    )


def pull_outcome(
    result: dict, added: dict[str, set[int]], changed: dict[str, set[int]], reviewers: list[dict]
) -> tuple[dict, list[dict]]:
    """One PR's outcomes.json entry and its reviewer_comments.csv rows.

    `added`: the lines the replayed version adds. `changed`: the lines of the replayed version
    that changed or went away before the merge. The line counts feed the base-rate test: do the
    lines the bot flagged change more often than the other added lines?
    """
    bot = result["comments"]
    flagged = {
        (comment["file"], line)
        for comment in bot
        for line in range(comment["start_line"], comment["line"] + 1)
        if line in added.get(comment["file"], set())
    }
    added_lines = {(path, line) for path, lines in added.items() for line in lines}
    changed_lines = {(path, line) for path, lines in changed.items() for line in lines}
    outcome = {
        "added": len(added_lines),
        "added_changed": len(added_lines & changed_lines),
        "flagged": len(flagged),
        "flagged_changed": len(flagged & changed_lines),
        "comments": {
            str(comment["id"]): {
                "changed_before_merge": any(
                    line in changed.get(comment["file"], set())
                    for line in range(comment["start_line"], comment["line"] + 1)
                ),
                "reviewer_comment_ids": [r["id"] for r in reviewers if near(comment, r)],
            }
            for comment in bot
        },
    }
    rows = [
        {
            "k": result["k"],
            "comment_id": reviewer["id"],
            "url": reviewer["url"],
            "file": reviewer["file"],
            "line": reviewer["line"],
            "text": reviewer["text"],
            "problem": "",
            # The first bot comment near it, if any; "same_issue" is then judged by hand.
            "bot_comment_url": next((c["url"] for c in bot if near(c, reviewer)), ""),
            "same_issue": "",
        }
        for reviewer in reviewers
    ]
    return outcome, rows


def outcome_for(http: httpx.Client, result: dict) -> tuple[dict, list[dict]]:
    """Fetch what `pull_outcome` needs for one collected PR: the upstream PR's commits (into
    the local clone) and its review comments."""
    upstream = result["upstream"]
    repo, number = upstream["repo"], upstream["number"]
    work = WORK_DIR / repo.split("/")[1]
    final = fetch_upstream_pull(work, f"{GITHUB_URL}/{repo}.git", number)
    added = diff_lines(work, upstream["merge_base_sha"], upstream["replay_sha"], "new")
    changed = diff_lines(work, upstream["replay_sha"], final, "old")
    author = get(http, f"/repos/{repo}/pulls/{number}").json()["user"]["login"]
    comments = list(iter_items(http, f"/repos/{repo}/pulls/{number}/comments"))
    reviewers = reviewer_comments(comments, author, upstream["replay_sha"])
    return pull_outcome(result, added, changed, reviewers)


def outcomes_for(
    http: httpx.Client, results: list[dict], previous: dict
) -> tuple[dict, list[dict]]:
    """Every collected PR's outcome and reviewer rows. An upstream PR that GitHub no longer
    serves (404: deleted, or hidden with its author's account) keeps the outcome worked out on
    an earlier run, from `previous`; its reviewer rows are already in the sheet. One that was
    never worked out is left out, and said so."""
    outcomes, rows = {}, []
    for result in results:
        k = str(result["k"])
        upstream = f"{result['upstream']['repo']}#{result['upstream']['number']}"
        try:
            outcome, pull_rows = outcome_for(http, result)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            if k in previous:
                outcomes[k] = previous[k]
                print(f"Replay {k}: {upstream} is gone from GitHub; kept its earlier outcome.")
            else:
                print(f"Replay {k}: {upstream} is gone from GitHub, and has no earlier outcome.")
            continue
        outcomes[k] = outcome
        rows += pull_rows
        matched = sum(1 for c in outcome["comments"].values() if c["reviewer_comment_ids"])
        print(
            f"Replay {k}: {len(outcome['comments'])} bot comment(s), {matched} near "
            f"a reviewer's; {len(pull_rows)} reviewer comment(s) on the replayed version"
        )
    return outcomes, rows


def main() -> None:
    paths = sorted(RESULTS_DIR.glob("*.json"), key=lambda path: int(path.stem))
    results = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    previous = {}
    if OUTCOMES_FILE.exists():
        previous = json.loads(OUTCOMES_FILE.read_text(encoding="utf-8"))
    with github_client(gh_token()) as http:
        outcomes, rows = outcomes_for(http, results, previous)
    OUTCOMES_FILE.write_text(json.dumps(outcomes, indent=1) + "\n", encoding="utf-8")
    written = merge_rows(REVIEWER_FILE, REVIEWER_COLUMNS, rows)
    todo = sum(1 for row in written if not row["problem"].strip())
    print(f"Wrote {OUTCOMES_FILE}.")
    print(f"{REVIEWER_FILE}: {todo} reviewer comment(s) still to mark (problem: yes or no).")


if __name__ == "__main__":
    main()
