"""Collecting a replayed PR's result: the run from the database, the review from GitHub, the
budget-limited flag, and the label sheet."""

import csv

import httpx
import pytest

from app.agent.loop import AgentStep as AgentStepRecord
from app.agent.verify import NOT_VERIFIED_DAILY_BUDGET, NOT_VERIFIED_PR_BUDGET
from app.db.models import RunStatus
from app.db.queries import (
    add_agent_step,
    finish_run,
    get_or_create_run,
    mark_posted,
    record_findings,
    set_verdict,
)
from app.github.webhooks import PullRequestPayload
from realworld import collect
from realworld.collect import build_result, fetch_review, load_run, update_labels
from realworld.common import github_client
from tests.factories import PR_NUMBER, REPO, SHA_1, SHA_2, pr_payload
from tests.test_db import candidate

REVIEW_ID = 900
PULL_URL = f"/repos/{REPO}/pulls/{PR_NUMBER}"
REPLAY = {"repo": REPO, "pull": PR_NUMBER, "url": f"https://github.com/{REPO}/pull/7"} | {
    "head_sha": SHA_1
}
ENTRY = {"k": 4, "repo": "Kludex/starlette", "number": 2501} | {
    "replay_sha": "c" * 40,
    "merge_base_sha": "e" * 40,
}
BODY = "### Automated review\nFound 1 confirmed issue(s): 1 high."


async def finished_run(sessionmaker, verdicts=("confirmed",), status=RunStatus.COMPLETED) -> int:
    """A run with one finding per verdict (None: not verified, with the reason given as a
    (None, reason) pair), finished with `status`."""
    async with sessionmaker() as session:
        run = await get_or_create_run(session, PullRequestPayload.model_validate(pr_payload()))
        ids = await record_findings(
            session, run.id, [candidate(f"finding {n}") for n in range(len(verdicts))], True
        )
        for finding_id, verdict in zip(ids, verdicts, strict=True):
            verdict, reason = verdict if isinstance(verdict, tuple) else (verdict, "checked")
            await set_verdict(session, finding_id, verdict, reason, [])
        await finish_run(session, run.id, status, github_review_id=REVIEW_ID)
    return run.id


def fetched(body: str = BODY, comments: list[dict] | None = None) -> dict:
    return {
        "pr_created_at": "2026-11-11T17:42:36Z",
        "review": {"id": REVIEW_ID, "url": "u", "submitted_at": "2026-11-11T17:46:25Z"}
        | {"body": body},
        "comments": comments or [],
    }


def comment(comment_id: int = 1, file: str = "a.py", line: int = 3, body: str = "**high** · bug"):
    return {"id": comment_id, "url": f"https://x/{comment_id}", "file": file, "line": line} | {
        "start_line": line,
        "body": body,
    }


async def test_a_run_is_loaded_with_its_findings_and_agent_steps(sessionmaker):
    assert await load_run(sessionmaker, REPLAY) == (None, None)
    await finished_run(sessionmaker, verdicts=("confirmed", "dropped"))
    async with sessionmaker() as session:
        await add_agent_step(session, 1, AgentStepRecord(1, "read_file", {}, 10, "ok", 100, 20, 5))
        await mark_posted(session, [1])

    run, findings = await load_run(sessionmaker, REPLAY)

    assert (run["status"], run["github_review_id"], run["head_sha"]) == ("completed", 900, SHA_1)
    assert [(f["verdict"], f["posted"]) for f in findings] == [
        ("confirmed", True),
        ("dropped", False),
    ]
    assert [step["tool"] for step in findings[0]["agent_steps"]] == ["read_file"]
    assert findings[1]["agent_steps"] == []
    # Another commit of the same PR is another run.
    assert await load_run(sessionmaker, REPLAY | {"head_sha": SHA_2}) == (None, None)


def test_the_review_and_its_comments_come_with_their_line_numbers(github_api):
    def answers(url, json):
        return github_api.get(url).mock(return_value=httpx.Response(200, json=json))

    answers(PULL_URL, {"created_at": "2026-11-11T17:42:36Z"})
    review = {"id": REVIEW_ID, "html_url": "review-url", "submitted_at": "2026-11-11T17:46:25Z"}
    answers(f"{PULL_URL}/reviews/{REVIEW_ID}", review | {"body": BODY})
    ours = {"id": 1, "html_url": "c1", "path": "a.py", "body": "**high** · bug · Divides by zero"}
    answers(
        f"{PULL_URL}/comments",
        [
            ours
            | {"pull_request_review_id": REVIEW_ID, "original_line": 12}
            | {"original_start_line": 10},
            ours
            | {"id": 2, "pull_request_review_id": REVIEW_ID, "original_line": 30}
            | {"original_start_line": None},
            # Someone else's comment on the same PR isn't part of the bot's review.
            ours
            | {"id": 3, "pull_request_review_id": 555, "original_line": 40}
            | {"original_start_line": None},
        ],
    )

    with github_client("token") as http:
        result = fetch_review(http, REPLAY, REVIEW_ID)

    assert result["pr_created_at"] == "2026-11-11T17:42:36Z"
    assert result["review"] == {"id": REVIEW_ID, "url": "review-url"} | {
        "submitted_at": "2026-11-11T17:46:25Z",
        "body": BODY,
    }
    assert [(c["id"], c["start_line"], c["line"]) for c in result["comments"]] == [
        (1, 10, 12),
        (2, 30, 30),
    ]


def test_a_run_that_posted_nothing_has_no_review(github_api):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json={"created_at": "t"}))

    with github_client("token") as http:
        assert fetch_review(http, REPLAY, None) == {"pr_created_at": "t"} | {
            "review": None,
            "comments": [],
        }


async def test_a_review_cut_down_by_a_daily_budget_is_not_scored(sessionmaker):
    await finished_run(sessionmaker, verdicts=("confirmed", (None, NOT_VERIFIED_DAILY_BUDGET)))
    run, findings = await load_run(sessionmaker, REPLAY)

    result = build_result(ENTRY, REPLAY, run, findings, fetched())

    assert result["budget_limited"] is True
    assert result["problems"] == ["finding 2 not verified (agent daily token budget)"]
    # The lanes' daily budget counts the same way.
    skipped = BODY + "\n- 2 lane call(s) not run (daily token budget), in: `a.py`"
    result = build_result(ENTRY, REPLAY, run, findings[:1], fetched(skipped))
    assert result["budget_limited"] is True


async def test_a_review_that_hit_only_the_per_pr_budget_is_scored(sessionmaker):
    await finished_run(sessionmaker, verdicts=("confirmed", (None, NOT_VERIFIED_PR_BUDGET)))
    run, findings = await load_run(sessionmaker, REPLAY)
    skipped = BODY + "\n- 3 lane call(s) not run (PR token budget), in: `a.py`"

    result = build_result(ENTRY, REPLAY, run, findings, fetched(skipped))

    assert result["budget_limited"] is False
    assert result["notes"] == [
        "3 lane call(s) not run (PR token budget)",
        "finding 2 not verified (agent PR token budget)",
    ]
    assert (result["k"], result["upstream"]["number"], result["fork"]["pull"]) == (4, 2501, 7)


async def test_a_run_that_did_not_complete_is_not_scored(sessionmaker):
    await finished_run(sessionmaker, status=RunStatus.FAILED)
    run, findings = await load_run(sessionmaker, REPLAY)

    result = build_result(ENTRY, REPLAY, run, findings, fetched() | {"review": None})

    assert result["budget_limited"] is True


async def test_a_link_to_a_github_issue_in_the_review_is_reported(sessionmaker):
    await finished_run(sessionmaker)
    run, findings = await load_run(sessionmaker, REPLAY)
    linked = comment(body="See https://github.com/Kludex/starlette/issues/2501 for why")

    clean = build_result(ENTRY, REPLAY, run, findings, fetched(comments=[comment()]))
    result = build_result(ENTRY, REPLAY, run, findings, fetched(comments=[linked]))

    assert clean["issue_links"] == []
    assert result["issue_links"] == ["github.com/Kludex/starlette/issues/2501"]


@pytest.fixture
def labels_file(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "LABELS_FILE", tmp_path / "labels.csv")
    return tmp_path / "labels.csv"


def read_labels(path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def test_new_comments_get_a_row_and_filled_in_labels_are_kept(labels_file):
    first = {"k": 2, "comments": [comment(11, body="**medium** · bug · Swallows errors\n\nWhy.")]}
    assert update_labels([first]) == 1
    [row] = read_labels(labels_file)
    assert (row["k"], row["comment_id"], row["file"], row["line"]) == ("2", "11", "a.py", "3")
    assert (row["title"], row["label"], row["url"]) == (
        "medium · bug · Swallows errors",
        "",
        "https://x/11",
    )

    # The owner fills in a label; the next collection adds another PR's comments.
    row |= {"label": "real", "note": "the except is too broad"}
    with labels_file.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, collect.LABEL_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
    second = {"k": 1, "comments": [comment(12, file="b.py", line=9), comment(13, line=1)]}

    assert update_labels([first, second]) == 2

    rows = read_labels(labels_file)
    # In PR order, then by file and line; nothing duplicated, the label still there.
    assert [r["comment_id"] for r in rows] == ["13", "12", "11"]
    assert (rows[2]["label"], rows[2]["note"]) == ("real", "the except is too broad")
