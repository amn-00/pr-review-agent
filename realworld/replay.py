"""Replay the sampled pull requests in the owner's forks, so the deployed bot reviews them
(docs/plans/real-world-eval.md, section 2).

    $env:PROD_DATABASE_URL = (Get-Clipboard -Raw).Trim()   # the Neon URL, as in docs/deploy.md
    $env:PGSSLROOTCERT = (uv run python -m certifi)
    uv run python -m realworld.replay --max 4 [--dry-run]

One PR at a time: check today's token budgets, push one new commit to the fork, open the PR
there, and wait for the bot's run to finish. It stops for the day when a budget is short, so
no review is cut down by the quota. Running it again is safe: it carries on after the last PR
it opened (realworld/replays.json).

It only ever pushes to and opens PRs in the owner's forks, and nothing it writes (branch, commit
message, PR title or body) refers to the upstream PR. --dry-run does the budget check and the
local git work, and pushes and opens nothing.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.verify import AGENT_DAILY_TOKEN_BUDGET, AGENT_PR_TOKEN_BUDGET
from app.db.models import (
    BudgetPurpose,
    DailyTokenUsage,
    PullRequest,
    Repo,
    ReviewRun,
    RunStatus,
)
from app.pipeline.orchestrator import PIPELINE_DAILY_TOKEN_BUDGET, PR_TOKEN_BUDGET, utc_today
from realworld.common import OWNER, gh_token, github_client, prod_database_url, read_only_engine
from realworld.select import PRS_FILE

REPLAYS_FILE = Path(__file__).parent / "replays.json"
# Local clones of the upstream repos (not committed).
WORK_DIR = Path(__file__).parent / "work"
GITHUB_URL = "https://github.com"
# The replay commit's author and date are fixed, so making it again gives the same commit. A
# run that died between the push and opening the PR can simply be run again.
REPLAY_IDENTITY = {
    "GIT_AUTHOR_NAME": "pr-review-agent replay",
    "GIT_AUTHOR_EMAIL": "replay@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "pr-review-agent replay",
    "GIT_COMMITTER_EMAIL": "replay@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}
# How long to wait for one review: the job timeout (900 s), plus Render's cold start and one
# reconciler cycle if the waking webhook was lost.
WAIT_SECONDS = 25 * 60
POLL_SECONDS = 15
FINISHED = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.SKIPPED}


def fork_of(upstream: str) -> str:
    """The owner's fork of an upstream repo, e.g. encode/httpx -> amn-00/httpx."""
    return f"{OWNER}/{upstream.split('/')[1]}"


def require_fork(repo: str) -> None:
    """Refuse any repo that isn't the owner's. A PR or a push anywhere else would land in front
    of the upstream's maintainers."""
    if repo.split("/")[0] != OWNER:
        raise ValueError(f"refusing to touch {repo}: replays only go to {OWNER}'s forks")


def replay_title(k: int, total: int) -> str:
    """The commit message and the PR title. No upstream PR number: a reference would add a
    notice on the maintainers' PR."""
    return f"Replay {k} of {total}"


def git(work: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(work), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def make_replay_commit(work: Path, upstream_url: str, entry: dict, message: str) -> str:
    """Fetch the upstream PR into the local clone `work`, and return a new commit holding the
    replayed version's files on top of the merge base.

    The upstream commits themselves are never pushed: their messages often say "#123" or
    "Fixes #123". The new commit has the same files on the same base, so the PR's diff is
    identical, and the bot reads the diff and the files, never the commit messages.
    """
    if not (work / ".git").exists():
        work.mkdir(parents=True, exist_ok=True)
        git(work, "init", "--quiet")
    number = entry["number"]
    # Into a ref, so git's garbage collection keeps the commits.
    refspec = f"+refs/pull/{number}/head:refs/upstream/pull/{number}"
    git(work, "fetch", "--quiet", upstream_url, refspec)
    tree = f"{entry['replay_sha']}^{{tree}}"
    return git(
        work, "commit-tree", tree, "-p", entry["merge_base_sha"], "-m", message, env=REPLAY_IDENTITY
    )


def push_replay(work: Path, fork: str, k: int, commit: str, merge_base_sha: str) -> None:
    """Two branches in the fork: the PR's base, at the merge base, and its head."""
    require_fork(fork)
    git(
        work,
        "push",
        "--quiet",
        f"{GITHUB_URL}/{fork}.git",
        f"{merge_base_sha}:refs/heads/replay/{k}-base",
        f"{commit}:refs/heads/replay/{k}",
    )


def open_pull(http: httpx.Client, fork: str, k: int, total: int) -> dict:
    """Open the PR between the two branches, inside the fork. If it's already there (an earlier
    run died right after opening it), that one is returned and nothing is opened."""
    require_fork(fork)
    head = f"replay/{k}"
    existing = http.get(f"/repos/{fork}/pulls", params={"head": f"{OWNER}:{head}", "state": "all"})
    existing.raise_for_status()
    if existing.json():
        return existing.json()[0]
    title = replay_title(k, total)
    response = http.post(
        f"/repos/{fork}/pulls",
        json={
            "title": title,
            "body": f"{title} for an evaluation. Not for merging.",
            "head": head,
            "base": f"{head}-base",
        },
    )
    response.raise_for_status()
    return response.json()


def open_replay(http: httpx.Client, entry: dict, total: int, dry_run: bool) -> dict | None:
    """Push one sampled PR to the fork and open it. Returns its replays.json record, or None
    on a dry run, which stops before the push."""
    upstream, k = entry["repo"], entry["k"]
    fork = fork_of(upstream)
    work = WORK_DIR / upstream.split("/")[1]
    upstream_url = f"{GITHUB_URL}/{upstream}.git"
    commit = make_replay_commit(work, upstream_url, entry, replay_title(k, total))
    if dry_run:
        print(f"Replay {k}: would push {commit[:7]} to {fork} and open a PR there.")
        return None
    push_replay(work, fork, k, commit, entry["merge_base_sha"])
    pull = open_pull(http, fork, k, total)
    return {
        "repo": fork,
        "pull": pull["number"],
        "url": pull["html_url"],
        "head_sha": commit,
        "opened_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def budget_shortfall(sessionmaker: async_sessionmaker) -> str | None:
    """Why the next PR must wait for another day, or None if it can start.

    A review that runs short of a daily budget is still posted, with lane calls "not run" or
    findings "not verified", and that would count against the bot for a quota reason. So a PR
    starts only when each daily budget has room for the most one attempt can spend: the per-PR
    budgets. It's the eval runner's pre-flight, for prod.
    """
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(DailyTokenUsage).where(DailyTokenUsage.day == utc_today())
        )
        for row in rows:
            if row.purpose == BudgetPurpose.PIPELINE:
                left, needed = PIPELINE_DAILY_TOKEN_BUDGET - row.tokens, PR_TOKEN_BUDGET
            else:
                left, needed = AGENT_DAILY_TOKEN_BUDGET - row.tokens, AGENT_PR_TOKEN_BUDGET
            if left < needed:
                return (
                    f"{row.model} has {left} {row.purpose} tokens left today; one review may "
                    f"need {needed}"
                )
    return None


async def run_status(sessionmaker: async_sessionmaker, replay: dict) -> str | None:
    """The status of the bot's run for a replayed PR; None if it has no run yet."""
    async with sessionmaker() as session:
        return await session.scalar(
            select(ReviewRun.status)
            .join(PullRequest, ReviewRun.pr_id == PullRequest.id)
            .join(Repo, PullRequest.repo_id == Repo.id)
            .where(
                Repo.full_name == replay["repo"],
                PullRequest.number == replay["pull"],
                ReviewRun.head_sha == replay["head_sha"],
            )
        )


async def wait_for_run(
    sessionmaker: async_sessionmaker,
    replay: dict,
    wait_seconds: float = WAIT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> str | None:
    """The run's status once it has finished, or whatever it is when the wait is over."""
    deadline = time.monotonic() + wait_seconds
    while True:
        status = await run_status(sessionmaker, replay)
        if status in FINISHED or time.monotonic() >= deadline:
            return status
        await asyncio.sleep(poll_seconds)


def read_replays() -> dict[str, dict]:
    """The PRs opened so far: k -> {repo, pull, url, head_sha, opened_at}."""
    if not REPLAYS_FILE.exists():
        return {}
    return json.loads(REPLAYS_FILE.read_text(encoding="utf-8"))


def write_replays(replays: dict[str, dict]) -> None:
    REPLAYS_FILE.write_text(json.dumps(replays, indent=1) + "\n", encoding="utf-8")


async def run(sample: list[dict], replays: dict[str, dict], max_prs: int, dry_run: bool) -> None:
    engine = read_only_engine(prod_database_url())
    sessionmaker = async_sessionmaker(engine)
    try:
        with github_client(gh_token()) as http:
            if replays:
                # One review at a time: the last PR's run must be over before the next starts.
                last = max(replays, key=int)
                status = await wait_for_run(sessionmaker, replays[last])
                if status != RunStatus.COMPLETED:
                    sys.exit(f"Replay {last}'s run is {status}, not completed. Look at it first.")
            todo = [entry for entry in sample if str(entry["k"]) not in replays]
            for entry in todo[:max_prs]:
                k = entry["k"]
                shortfall = await budget_shortfall(sessionmaker)
                if shortfall:
                    print(f"Stopping for today, before replay {k}: {shortfall}.")
                    break
                opened = open_replay(http, entry, len(sample), dry_run)
                if opened is None:
                    continue
                # Saved before the wait, so the PR is on record even if the wait is interrupted.
                replays[str(k)] = opened
                write_replays(replays)
                print(f"Replay {k}: opened {opened['url']}, waiting for the review ...", flush=True)
                status = await wait_for_run(sessionmaker, opened)
                print(f"Replay {k}: run {status}")
                if status != RunStatus.COMPLETED:
                    print("Stopping: look at that run before replaying more.")
                    break
    finally:
        await engine.dispose()
    print(f"{len(replays)} of {len(sample)} replayed so far.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--max", type=int, default=4, help="PRs to open today, at most")
    parser.add_argument("--dry-run", action="store_true", help="push and open nothing")
    args = parser.parse_args()
    sample = json.loads(PRS_FILE.read_text(encoding="utf-8"))["sample"]
    asyncio.run(run(sample, read_replays(), args.max, args.dry_run))


if __name__ == "__main__":
    main()
