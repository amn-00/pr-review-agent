"""Collect what the deployed bot did on each replayed PR (docs/plans/real-world-eval.md,
section 3).

    $env:PROD_DATABASE_URL = (Get-Clipboard -Raw).Trim()   # the Neon URL, as in docs/deploy.md
    $env:PGSSLROOTCERT = (uv run python -m certifi)
    uv run python -m realworld.collect

For every PR in realworld/replays.json it reads the run, every candidate with its verdict and
agent steps from Neon (read-only), and the posted review with its inline comments from GitHub.
It writes realworld/results/<k>.json, and adds each new inline comment to realworld/labels.csv
for judging by hand. It's safe to run again: results are rewritten, and labels already filled
in are kept.
"""

import asyncio
import csv
import json
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AgentStep, Finding, PullRequest, Repo, ReviewRun
from evals.run import completeness
from realworld.common import (
    get,
    gh_token,
    github_client,
    iter_items,
    prod_database_url,
    read_only_engine,
)
from realworld.replay import read_replays
from realworld.select import ISSUE_LINK, PRS_FILE

RESULTS_DIR = Path(__file__).parent / "results"
LABELS_FILE = Path(__file__).parent / "labels.csv"
LABEL_COLUMNS = ["k", "comment_id", "url", "file", "line", "title", "label", "note"]


def _columns(row) -> dict:
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


async def load_run(
    sessionmaker: async_sessionmaker, replay: dict
) -> tuple[dict, list[dict]] | tuple[None, None]:
    """The bot's run for a replayed PR and its findings, each with its agent steps, as plain
    dicts. (None, None) if the PR has no run."""
    async with sessionmaker() as session:
        run = await session.scalar(
            select(ReviewRun)
            .join(PullRequest, ReviewRun.pr_id == PullRequest.id)
            .join(Repo, PullRequest.repo_id == Repo.id)
            .where(
                Repo.full_name == replay["repo"],
                PullRequest.number == replay["pull"],
                ReviewRun.head_sha == replay["head_sha"],
            )
        )
        if run is None:
            return None, None
        rows = list(
            await session.scalars(
                select(Finding).where(Finding.run_id == run.id).order_by(Finding.id)
            )
        )
        steps = list(
            await session.scalars(
                select(AgentStep)
                .where(AgentStep.finding_id.in_([row.id for row in rows]))
                .order_by(AgentStep.finding_id, AgentStep.step_no)
            )
        )
    findings = []
    for row in rows:
        finding = _columns(row)
        finding["agent_steps"] = [_columns(step) for step in steps if step.finding_id == row.id]
        findings.append(finding)
    return _columns(run), findings


def fetch_review(http: httpx.Client, replay: dict, review_id: int | None) -> dict:
    """The fork PR's opening time, and the review the run posted with its inline comments.
    `review_id` is the run's `github_review_id`: the exact review, not a guess by author."""
    pull_url = f"/repos/{replay['repo']}/pulls/{replay['pull']}"
    fetched = {"pr_created_at": get(http, pull_url).json()["created_at"]}
    if review_id is None:  # the run posted nothing
        return fetched | {"review": None, "comments": []}
    review = get(http, f"{pull_url}/reviews/{review_id}").json()
    # The PR's comment list, filtered to this review: GitHub's list of one review's comments
    # leaves out their line numbers.
    comments = [
        comment
        for comment in iter_items(http, f"{pull_url}/comments")
        if comment["pull_request_review_id"] == review_id
    ]
    fetched["review"] = {
        "id": review["id"],
        "url": review["html_url"],
        "submitted_at": review["submitted_at"],
        "body": review["body"],
    }
    fetched["comments"] = [
        {
            "id": comment["id"],
            "url": comment["html_url"],
            "file": comment["path"],
            # The comment's place in the version it was made on, which is the replayed one.
            "line": comment["original_line"],
            "start_line": comment["original_start_line"] or comment["original_line"],
            "body": comment["body"],
        }
        for comment in comments
    ]
    return fetched


def build_result(entry: dict, replay: dict, run: dict, findings: list[dict], fetched: dict) -> dict:
    """One replayed PR's result file. `budget_limited` uses the eval runner's rule for an
    incomplete run: a lane call not run for the daily budget, a finding not verified for the
    agent's daily budget, a failed lane call, or a run that didn't complete. Such a review is
    listed in the report and left out of the scores. Skips for the per-PR budget or the time
    limit are the bot's normal behaviour: they're `notes`, and the review is scored."""
    problems, notes = completeness(run, findings, fetched["review"], None)
    review_body = fetched["review"]["body"] if fetched["review"] else ""
    posted_text = "\n".join([review_body, *(comment["body"] for comment in fetched["comments"])])
    return {
        "k": entry["k"],
        "upstream": {key: entry[key] for key in ("repo", "number", "replay_sha", "merge_base_sha")},
        "fork": {key: replay[key] for key in ("repo", "pull", "url", "head_sha")},
        "budget_limited": bool(problems),
        "problems": problems,
        "notes": notes,
        # A link to a GitHub issue in what the bot posted would add a cross-reference there.
        "issue_links": sorted({match.group(0) for match in ISSUE_LINK.finditer(posted_text)}),
        "run": run,
        "findings": findings,
        **fetched,
    }


def write_result(result: dict) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{result['k']}.json"
    path.write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")


def update_labels(results: list[dict]) -> int:
    """Add a row to labels.csv for every inline comment that has none yet, keeping the labels
    and notes already filled in. Returns how many rows still have no label."""
    rows: dict[str, dict] = {}
    if LABELS_FILE.exists():
        with LABELS_FILE.open(encoding="utf-8-sig", newline="") as file:
            rows = {row["comment_id"]: row for row in csv.DictReader(file)}
    for result in results:
        for comment in result["comments"]:
            rows.setdefault(
                str(comment["id"]),
                {
                    "k": result["k"],
                    "comment_id": comment["id"],
                    "url": comment["url"],
                    "file": comment["file"],
                    "line": comment["line"],
                    # The comment's first line: "**medium** · bug · <title>".
                    "title": comment["body"].splitlines()[0].replace("**", ""),
                    "label": "",
                    "note": "",
                },
            )
    ordered = sorted(rows.values(), key=lambda row: (int(row["k"]), row["file"], int(row["line"])))
    # utf-8-sig: Excel reads the file as UTF-8 only with this marker.
    with LABELS_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, LABEL_COLUMNS)
        writer.writeheader()
        writer.writerows(ordered)
    return sum(1 for row in ordered if not row["label"].strip())


async def collect(sample: list[dict], replays: dict[str, dict]) -> list[dict]:
    """A result for every replayed PR whose run exists."""
    entries = {str(entry["k"]): entry for entry in sample}
    engine = read_only_engine(prod_database_url())
    sessionmaker = async_sessionmaker(engine)
    results = []
    try:
        with github_client(gh_token()) as http:
            for k, replay in sorted(replays.items(), key=lambda item: int(item[0])):
                run, findings = await load_run(sessionmaker, replay)
                if run is None:
                    print(f"Replay {k}: no run in the database yet, skipped.")
                    continue
                fetched = fetch_review(http, replay, run["github_review_id"])
                result = build_result(entries[k], replay, run, findings, fetched)
                write_result(result)
                results.append(result)
                state = "BUDGET-LIMITED, not scored" if result["budget_limited"] else "ok"
                details = "; ".join(result["problems"] + result["notes"])
                print(
                    f"Replay {k}: run {run['status']}, {len(result['comments'])} inline "
                    f"comment(s), {state}" + (f" ({details})" if details else "")
                )
                if result["issue_links"]:
                    print(f"  !! the review links to: {', '.join(result['issue_links'])}")
    finally:
        await engine.dispose()
    return results


def main() -> None:
    sample = json.loads(PRS_FILE.read_text(encoding="utf-8"))["sample"]
    results = asyncio.run(collect(sample, read_replays()))
    unlabelled = update_labels(results)
    comments = sum(len(result["comments"]) for result in results)
    print(f"{len(results)} result(s) in {RESULTS_DIR}; {comments} inline comment(s).")
    print(f"{LABELS_FILE}: {unlabelled} comment(s) still to label (real, wrong or nit).")


if __name__ == "__main__":
    main()
