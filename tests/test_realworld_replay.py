"""Replaying a sampled PR: only into the owner's fork, with nothing that refers to the upstream
PR, and only while the day's token budgets have room for a whole review."""

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.verify import AGENT_DAILY_TOKEN_BUDGET, AGENT_PR_TOKEN_BUDGET
from app.db.models import BudgetPurpose, RunStatus
from app.db.queries import add_daily_tokens, daily_tokens_used, finish_run, get_or_create_run
from app.github.webhooks import PullRequestPayload
from app.pipeline.orchestrator import PIPELINE_DAILY_TOKEN_BUDGET, PR_TOKEN_BUDGET, utc_today
from realworld import replay
from realworld.common import github_client, read_only_engine
from realworld.replay import (
    REPLAY_IDENTITY,
    budget_shortfall,
    fork_of,
    git,
    make_replay_commit,
    open_pull,
    push_replay,
    require_fork,
    wait_for_run,
)
from tests.conftest import TEST_DATABASE_URL
from tests.factories import PR_NUMBER, REPO, SHA_1, SHA_2, pr_payload

BIG, SMALL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
FORK = "amn-00/starlette"
# The bot's run for the PR that tests/factories.py's payload describes.
REPLAYED = {"repo": REPO, "pull": PR_NUMBER, "head_sha": SHA_1}


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    """A small upstream repo and an empty fork of it on disk, standing in for GitHub. The
    upstream has a base commit and a PR of three commits whose messages refer to issues; the
    reviewers first saw the second. Returns the PR's prs.json entry and its commits."""
    monkeypatch.setattr(replay, "GITHUB_URL", tmp_path.as_posix())
    repo = tmp_path / "encode" / "proj.git"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")

    def commit(message: str, **files: str) -> str:
        for name, text in files.items():
            (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", "--all")
        # Any author will do for the test repo's own commits; CI has none configured.
        signing_off = ("-c", "commit.gpgsign=false")
        git(repo, *signing_off, "commit", "--quiet", "-m", message, env=REPLAY_IDENTITY)
        return git(repo, "rev-parse", "HEAD")

    base = commit("Initial commit", **{"app.py": "a = 1\n"})
    commit("Fix the total (#123)", **{"app.py": "a = 2\n"})
    seen = commit("Add b. Fixes #124", **{"b.py": "b = 1\n"})
    final = commit("Address review of encode/proj#125", **{"b.py": "b = 2\n"})
    git(repo, "update-ref", "refs/pull/5/head", final)

    fork = tmp_path / "amn-00" / "proj.git"
    fork.mkdir(parents=True)
    git(fork, "init", "--quiet", "--bare")
    entry = {"repo": "encode/proj", "number": 5, "k": 1, "replay_sha": seen, "merge_base_sha": base}
    return {"entry": entry, "repo": repo, "fork": fork, "base": base, "seen": seen, "final": final}


def test_the_replay_commit_has_the_replayed_files_and_none_of_the_upstream_messages(
    tmp_path, upstream
):
    work = tmp_path / "work"
    url = f"{replay.GITHUB_URL}/encode/proj.git"

    commit = make_replay_commit(work, url, upstream["entry"], "Replay 1 of 30")

    # The files the reviewers first saw (not the final version), directly on the merge base.
    tree = git(work, "rev-parse", f"{commit}^{{tree}}")
    assert tree == git(work, "rev-parse", f"{upstream['seen']}^{{tree}}")
    assert tree != git(work, "rev-parse", f"{upstream['final']}^{{tree}}")
    assert git(work, "rev-list", "--parents", "-n", "1", commit) == f"{commit} {upstream['base']}"
    assert git(work, "log", "-1", "--format=%B", commit) == "Replay 1 of 30"
    # Its dates are fixed, not the clock's, so making it again (after a crash, say) gives the
    # same commit and there's nothing new to push.
    fixed = int(datetime.fromisoformat(REPLAY_IDENTITY["GIT_AUTHOR_DATE"]).timestamp())
    assert git(work, "log", "-1", "--format=%at %ct", commit) == f"{fixed} {fixed}"
    assert make_replay_commit(work, url, upstream["entry"], "Replay 1 of 30") == commit


def test_the_push_creates_the_two_branches_and_leaves_the_upstream_commits_behind(
    tmp_path, upstream
):
    work, fork = tmp_path / "work", upstream["fork"]
    url = f"{replay.GITHUB_URL}/encode/proj.git"
    commit = make_replay_commit(work, url, upstream["entry"], "Replay 1 of 30")

    push_replay(work, "amn-00/proj", 1, commit, upstream["base"])

    assert git(fork, "rev-parse", "refs/heads/replay/1") == commit
    assert git(fork, "rev-parse", "refs/heads/replay/1-base") == upstream["base"]
    # The PR is one commit with a neutral message; "#123" and "Fixes #124" never reach the fork.
    assert git(fork, "log", "--format=%s", "replay/1-base..replay/1") == "Replay 1 of 30"
    with pytest.raises(RuntimeError):
        git(fork, "cat-file", "-e", upstream["seen"])
    # And the PR's diff is exactly the diff the reviewers first saw.
    replayed = git(fork, "diff", "replay/1-base", "replay/1")
    assert replayed == git(upstream["repo"], "diff", upstream["base"], upstream["seen"])
    assert "b = 1" in replayed


def test_a_repo_that_is_not_the_owners_is_refused(tmp_path, github_api):
    assert fork_of("encode/httpx") == "amn-00/httpx"
    require_fork("amn-00/httpx")
    with pytest.raises(ValueError, match="refusing to touch encode/httpx"):
        require_fork("encode/httpx")
    # Refused before git or GitHub is touched: without the check, the push would fail as a
    # RuntimeError (tmp_path isn't a repo) and the request as an unmocked route.
    with pytest.raises(ValueError):
        push_replay(tmp_path, "encode/httpx", 1, SHA_1, SHA_2)
    with github_client("token") as http, pytest.raises(ValueError):
        open_pull(http, "encode/httpx", 1, 30)


def test_the_pr_is_opened_inside_the_fork_and_says_nothing_about_the_upstream(github_api):
    listing = github_api.get(f"/repos/{FORK}/pulls").mock(return_value=httpx.Response(200, json=[]))
    created = {"number": 3, "html_url": f"https://github.com/{FORK}/pull/3"}
    post = github_api.post(f"/repos/{FORK}/pulls").mock(
        return_value=httpx.Response(201, json=created)
    )

    with github_client("token") as http:
        pull = open_pull(http, FORK, 7, 30)

    assert pull == created
    assert listing.calls.last.request.url.params["head"] == "amn-00:replay/7"
    sent = json.loads(post.calls.last.request.content)
    assert sent == {
        "title": "Replay 7 of 30",
        "body": "Replay 7 of 30 for an evaluation. Not for merging.",
        "head": "replay/7",
        "base": "replay/7-base",
    }
    # Nothing GitHub could turn into a reference to the upstream PR.
    assert not any(mark in post.calls.last.request.content.decode() for mark in ("#", "encode"))


def test_a_pr_that_is_already_open_is_not_opened_again(github_api):
    existing = {"number": 3, "html_url": f"https://github.com/{FORK}/pull/3"}
    github_api.get(f"/repos/{FORK}/pulls").mock(return_value=httpx.Response(200, json=[existing]))
    post = github_api.post(f"/repos/{FORK}/pulls").mock(return_value=httpx.Response(201, json={}))

    with github_client("token") as http:
        assert open_pull(http, FORK, 7, 30) == existing

    assert not post.called


async def spend(sessionmaker, model: str, purpose: BudgetPurpose, tokens: int, days_ago: int = 0):
    async with sessionmaker() as session:
        day = utc_today() - timedelta(days=days_ago)
        await add_daily_tokens(session, model, day, purpose, tokens)


async def test_a_pr_starts_only_while_the_lanes_have_room_for_a_whole_review(sessionmaker):
    assert await budget_shortfall(sessionmaker) is None  # nothing spent today

    # Exactly the per-PR budget left on the big model: one more review fits.
    await spend(
        sessionmaker, BIG, BudgetPurpose.PIPELINE, PIPELINE_DAILY_TOKEN_BUDGET - PR_TOKEN_BUDGET
    )
    assert await budget_shortfall(sessionmaker) is None

    # The small model has its own budget, and it's the one that's short.
    await spend(
        sessionmaker,
        SMALL,
        BudgetPurpose.PIPELINE,
        PIPELINE_DAILY_TOKEN_BUDGET - PR_TOKEN_BUDGET + 1,
    )
    shortfall = await budget_shortfall(sessionmaker)
    assert shortfall.startswith(f"{SMALL} has {PR_TOKEN_BUDGET - 1} pipeline tokens left today")


async def test_a_pr_starts_only_while_the_agent_has_room_for_a_whole_review(sessionmaker):
    room = AGENT_DAILY_TOKEN_BUDGET - AGENT_PR_TOKEN_BUDGET
    await spend(sessionmaker, BIG, BudgetPurpose.AGENT, room)
    assert await budget_shortfall(sessionmaker) is None

    await spend(sessionmaker, BIG, BudgetPurpose.AGENT, 1)
    shortfall = await budget_shortfall(sessionmaker)
    assert shortfall.startswith(f"{BIG} has {AGENT_PR_TOKEN_BUDGET - 1} agent tokens left today")


async def test_yesterdays_spend_does_not_count(sessionmaker):
    await spend(sessionmaker, BIG, BudgetPurpose.AGENT, AGENT_DAILY_TOKEN_BUDGET, days_ago=1)
    await spend(sessionmaker, BIG, BudgetPurpose.PIPELINE, PIPELINE_DAILY_TOKEN_BUDGET, days_ago=1)

    assert await budget_shortfall(sessionmaker) is None


async def test_the_prod_engine_can_read_but_not_write(sessionmaker):
    await spend(sessionmaker, BIG, BudgetPurpose.AGENT, 500)
    engine = read_only_engine(TEST_DATABASE_URL)
    read_only = async_sessionmaker(engine)
    try:
        async with read_only() as session:
            assert await daily_tokens_used(session, BIG, utc_today(), BudgetPurpose.AGENT) == 500
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await spend(read_only, BIG, BudgetPurpose.AGENT, 1)
    finally:
        await engine.dispose()


async def create_run(sessionmaker) -> int:
    async with sessionmaker() as session:
        run = await get_or_create_run(session, PullRequestPayload.model_validate(pr_payload()))
    return run.id


async def test_the_wait_ends_when_the_run_has_finished(sessionmaker):
    # No run yet (the webhook hasn't arrived), then a queued one: the wait runs out.
    assert await wait_for_run(sessionmaker, REPLAYED, wait_seconds=0) is None
    run_id = await create_run(sessionmaker)
    assert await wait_for_run(sessionmaker, REPLAYED, wait_seconds=0) == RunStatus.QUEUED
    # Another commit of the same PR is another run.
    assert await wait_for_run(sessionmaker, REPLAYED | {"head_sha": SHA_2}, wait_seconds=0) is None

    # It keeps polling while the run is unfinished, and returns as soon as it's done.
    waiting = asyncio.create_task(wait_for_run(sessionmaker, REPLAYED, 30, poll_seconds=0.01))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    async with sessionmaker() as session:
        await finish_run(session, run_id, RunStatus.COMPLETED)
    async with asyncio.timeout(5):
        assert await waiting == RunStatus.COMPLETED


@pytest.fixture
def session_setup(monkeypatch, tmp_path):
    """replay.run() with GitHub and git stubbed out: open_replay records the PRs it was asked
    to open, and answers with the run the test created."""
    opened = []

    def open_replay(http, entry, total, dry_run):
        opened.append(entry["k"])
        return {**REPLAYED, "url": f"https://github.com/{FORK}/pull/{entry['k']}"}

    monkeypatch.setattr(replay, "open_replay", open_replay)
    monkeypatch.setattr(replay, "gh_token", lambda: "token")
    monkeypatch.setattr(replay, "prod_database_url", lambda: TEST_DATABASE_URL)
    monkeypatch.setattr(replay, "REPLAYS_FILE", tmp_path / "replays.json")
    return opened


SAMPLE = [{"k": k, "repo": "Kludex/starlette"} for k in range(1, 7)]


async def test_a_session_opens_up_to_max_prs_then_none_once_a_budget_is_short(
    sessionmaker, session_setup
):
    async with sessionmaker() as session:
        await finish_run(session, await create_run(sessionmaker), RunStatus.COMPLETED)
    replays = {}

    await replay.run(SAMPLE, replays, max_prs=2, dry_run=False)

    assert session_setup == [1, 2]
    assert list(replays) == ["1", "2"]
    assert json.loads(replay.REPLAYS_FILE.read_text(encoding="utf-8")) == replays

    # The next day's session carries on after the last PR, until the agent's budget is short.
    await spend(sessionmaker, BIG, BudgetPurpose.AGENT, AGENT_DAILY_TOKEN_BUDGET)
    await replay.run(SAMPLE, replays, max_prs=4, dry_run=False)

    assert session_setup == [1, 2]


async def test_a_session_stops_if_the_last_replay_did_not_complete(sessionmaker, session_setup):
    async with sessionmaker() as session:
        await finish_run(session, await create_run(sessionmaker), RunStatus.FAILED)

    with pytest.raises(SystemExit, match="Replay 2's run is failed"):
        await replay.run(SAMPLE, {"1": REPLAYED, "2": REPLAYED}, max_prs=4, dry_run=False)

    assert session_setup == []
