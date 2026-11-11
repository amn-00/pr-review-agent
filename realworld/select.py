"""Choose the pull requests for the real-world eval: a fixed rule, then a seeded random sample,
before the bot has seen any of them (docs/plans/real-world-eval.md, section 1).

    uv run python -m realworld.select

Writes realworld/prs.json: 10 PRs per repo to replay, and a second, disjoint 10 per repo held
back for judging later changes to the bot. Commit it before the first replay. It reads public
GitHub data only, as the owner (`gh auth login`), and takes a few minutes.
"""

import json
import random
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from pathlib import Path
from urllib.parse import quote

import httpx

from app.github.diff import parse_patch
from app.pipeline.chunking import build_chunks, priority, skip_reason
from app.pipeline.lanes import LANES, estimate_call_tokens
from app.pipeline.orchestrator import PR_TOKEN_BUDGET
from realworld.common import get, gh_token, github_client, iter_items

# Their current names: Starlette moved from encode/ to Kludex/. Under the old name every API
# request is redirected, and counts twice against the rate limit.
# Two repos, not the plan's three: on 2026-11-11 encode/httpx had no PR that passes the rule,
# and the backups too few (pallets/click 9, Kludex/uvicorn 7). These two had 25 and 29.
REPOS = ["Kludex/starlette", "pydantic/pydantic-settings"]
PRS_FILE = Path(__file__).parent / "prs.json"
SEED = 20261111
# Replayed per repo. The held-back set is whatever qualifies beyond these, up to as many again.
PER_REPO = 15
# Opened this recently, so the code and the review habits are current.
WINDOW = timedelta(days=2 * 365)
# The size of the replayed diff. A guess at what fits the per-PR token budget: main() prints
# each sampled PR's estimate so it can be checked before the sample is committed.
MAX_LINES = 300
MAX_FILES = 10
RELEASE_TITLE = re.compile(r"^\W*(release|version|bump|prepare release)\b", re.IGNORECASE)
# A link to any GitHub issue or PR, as a URL or as owner/repo#123.
ISSUE_LINK = re.compile(r"github\.com/[\w.-]+/[\w.-]+/(issues|pull)/\d+|[\w.-]+/[\w.-]+#\d+")


def is_person(user: dict | None) -> bool:
    """Not a bot, and not a deleted account (GitHub returns no user for those)."""
    return bool(user) and user["type"] == "User" and not user["login"].endswith("[bot]")


def pull_reject_reason(pull: dict) -> str | None:
    """The rules that need only the PR itself, so they cost no extra requests."""
    if not pull["merged_at"]:
        return "not merged"
    if not is_person(pull["user"]):
        return "opened by a bot"
    if RELEASE_TITLE.search(pull["title"]):
        return "release or version bump"
    return None


def first_human_review(pull: dict, reviews: list[dict]) -> dict | None:
    """The earliest submitted review by a person other than the PR's author. Its `commit_id` is
    the version the reviewers first saw, which is the version we replay."""
    author = pull["user"]["login"]
    by_others = [
        review
        for review in reviews
        if is_person(review["user"])
        and review["user"]["login"] != author
        and review.get("submitted_at")
    ]
    # GitHub's timestamps are all UTC in one format, so they sort as text.
    return min(by_others, key=lambda review: review["submitted_at"], default=None)


def review_reject_reason(
    pull: dict, reviews: list[dict], comments: list[dict], commit_shas: list[str]
) -> str | None:
    """`comments` are the PR's line comments; `commit_shas` the commits it ended up with."""
    author = pull["user"]["login"]
    if not any(is_person(c["user"]) and c["user"]["login"] != author for c in comments):
        return "no line comment from a reviewer"
    review = first_human_review(pull, reviews)
    if review is None:
        return "no review by a person other than the author"
    # A force-push after the review replaces the commit the reviewers saw. It can't be fetched
    # through the PR any more, so the PR can't be replayed as they saw it.
    if review["commit_id"] not in commit_shas:
        return "the first review's commit was force-pushed away"
    return None


def diff_reject_reason(files: list[dict]) -> str | None:
    """`files` are the replayed diff's files, as GitHub's compare API lists them."""
    if len(files) > MAX_FILES:
        return f"more than {MAX_FILES} files"
    if sum(file["additions"] + file["deletions"] for file in files) > MAX_LINES:
        return f"more than {MAX_LINES} changed lines"
    if not any(_is_source_python(file) for file in files):
        return "no .py file outside tests and docs"
    # The bot's comments are model text. A comment that repeated such a link would add a
    # cross-reference on that issue, in the upstream repo or anyone else's.
    if any(ISSUE_LINK.search(file.get("patch") or "") for file in files):
        return "links to a GitHub issue or PR"
    return None


def _is_source_python(file: dict) -> bool:
    path = file["filename"]
    return file["status"] != "removed" and path.endswith(".py") and priority(path) == 0


def find_qualifying(
    http: httpx.Client, repo: str, since: datetime
) -> tuple[list[dict], dict[int, list[dict]], Counter]:
    """Every PR of `repo` opened since `since` that passes the rule, as prs.json entries; the
    replayed diff's files of each (for the token estimate); and how many PRs each part of the
    rule turned away."""
    entries: list[dict] = []
    files_by_number: dict[int, list[dict]] = {}
    rejected: Counter = Counter()
    newest_first = {"state": "closed", "sort": "created", "direction": "desc"}
    for pull in iter_items(http, f"/repos/{repo}/pulls", newest_first):
        if datetime.fromisoformat(pull["created_at"]) < since:
            break  # the rest are older still
        url = f"/repos/{repo}/pulls/{pull['number']}"
        reason = pull_reject_reason(pull)
        if not reason:
            reviews = list(iter_items(http, f"{url}/reviews"))
            comments = list(iter_items(http, f"{url}/comments"))
            commit_shas = [commit["sha"] for commit in iter_items(http, f"{url}/commits")]
            reason = review_reject_reason(pull, reviews, comments, commit_shas)
        if not reason:
            replay_sha = first_human_review(pull, reviews)["commit_id"]
            # Three dots: the diff from the merge base, which is what a PR shows.
            compare = get(
                http, f"/repos/{repo}/compare/{pull['base']['sha']}...{replay_sha}"
            ).json()
            files = compare.get("files", [])
            reason = diff_reject_reason(files)
        if reason:
            rejected[reason] += 1
            continue
        entries.append(
            {
                "repo": repo,
                "number": pull["number"],
                "replay_sha": replay_sha,
                "merge_base_sha": compare["merge_base_commit"]["sha"],
                "files": len(files),
                "lines": sum(file["additions"] + file["deletions"] for file in files),
            }
        )
        files_by_number[pull["number"]] = files
    return entries, files_by_number, rejected


def draw(entries: list[dict], seed: int, repo: str) -> tuple[list[dict], list[dict]]:
    """A seeded shuffle of one repo's qualifying PRs: the first PER_REPO are replayed, and the
    next PER_REPO (or as many as are left) are held back. The repo is part of the seed, so each
    repo has its own shuffle."""
    shuffled = sorted(entries, key=lambda entry: entry["number"])
    random.Random(f"{seed}:{repo}").shuffle(shuffled)
    return shuffled[:PER_REPO], shuffled[PER_REPO : 2 * PER_REPO]


def interleave(samples: list[list[dict]]) -> list[dict]:
    """One list, taking a PR from each repo in turn, numbered from 1 (`k`). If the replays stop
    early, every repo is still represented."""
    mixed = [entry for group in zip_longest(*samples) for entry in group if entry is not None]
    return [{"k": k, **entry} for k, entry in enumerate(mixed, start=1)]


def lane_tokens(files: list[dict], contents: dict[str, str]) -> int:
    """What the lanes would spend on this diff: the app's own estimate, over the chunks the
    review job would build. `contents` maps each reviewed file to its text at the replayed
    commit."""
    total = 0
    for file in files:
        path = file["filename"]
        if path not in contents:
            continue
        for chunk in build_chunks(path, parse_patch(file["patch"]), contents[path]):
            total += sum(estimate_call_tokens(lane, chunk) for lane in LANES)
    return total


def fetch_contents(http: httpx.Client, repo: str, sha: str, files: list[dict]) -> dict[str, str]:
    """The text of each file the review job would review, at `sha`."""
    contents = {}
    for file in files:
        # The review job's own rules: no deleted files, lockfiles, binaries or pure deletions.
        if skip_reason(file) or not parse_patch(file["patch"]):
            continue
        response = get(
            http,
            f"/repos/{repo}/contents/{quote(file['filename'])}",
            params={"ref": sha},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        contents[file["filename"]] = response.text
    return contents


def main() -> None:
    if PRS_FILE.exists():
        sys.exit(
            f"{PRS_FILE} already exists. The sample is drawn once, before any replay. Delete "
            "the file yourself to draw again, and only if nothing has been replayed yet."
        )
    now = datetime.now(UTC)
    samples, reserve, repos = [], [], {}
    with github_client(gh_token()) as http:
        for repo in REPOS:
            print(f"{repo}: reading merged PRs since {now - WINDOW:%Y-%m-%d} ...", flush=True)
            entries, files_by_number, rejected = find_qualifying(http, repo, now - WINDOW)
            sample, held_back = draw(entries, SEED, repo)
            for entry in sample + held_back:
                files = files_by_number[entry["number"]]
                contents = fetch_contents(http, repo, entry["replay_sha"], files)
                entry["lane_tokens"] = lane_tokens(files, contents)
            samples.append(sample)
            reserve += held_back
            repos[repo] = {"qualifying": len(entries), "rejected": dict(rejected)}
            print(f"  {len(entries)} qualify; {len(sample)} sampled, {len(held_back)} held back")
            for reason, count in rejected.most_common():
                print(f"  turned away, {reason}: {count}")
            if len(sample) < PER_REPO:
                print(f"  !! fewer than {PER_REPO} qualify: this repo can't fill its sample")

    data = {
        "drawn_at": now.isoformat(timespec="seconds"),
        "seed": SEED,
        "rule": {
            "window_days": WINDOW.days,
            "max_lines": MAX_LINES,
            "max_files": MAX_FILES,
            "per_repo": PER_REPO,
        },
        "repos": repos,
        "sample": interleave(samples),
        "reserve": reserve,
    }
    PRS_FILE.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")

    print(f"\nWrote {PRS_FILE}. The sample, with the lanes' estimated tokens:")
    for entry in data["sample"]:
        over = "  !! over the per-PR budget" if entry["lane_tokens"] > PR_TOKEN_BUDGET else ""
        print(
            f"  {entry['k']:>2}  {entry['repo']}#{entry['number']}: {entry['files']} file(s), "
            f"{entry['lines']} line(s), ~{entry['lane_tokens']} tokens{over}"
        )
    print(f"The per-PR budget is {PR_TOKEN_BUDGET}. Check the estimates, then commit the file.")


if __name__ == "__main__":
    main()
