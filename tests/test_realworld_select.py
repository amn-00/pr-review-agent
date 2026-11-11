"""Choosing the real-world eval's PRs: the rule, the seeded sample and the token estimate."""

from datetime import UTC, datetime

import httpx
import pytest

from app.github.diff import parse_patch
from app.pipeline.chunking import build_chunks
from app.pipeline.lanes import LANES, estimate_call_tokens
from evals.fake_github import make_patch
from realworld import common
from realworld.common import github_client
from realworld.select import (
    MAX_FILES,
    MAX_LINES,
    PER_REPO,
    diff_reject_reason,
    draw,
    fetch_contents,
    find_qualifying,
    first_human_review,
    interleave,
    lane_tokens,
    pull_reject_reason,
    review_reject_reason,
)

REPO = "Kludex/starlette"
LINKED = "links to a GitHub issue or PR"
AUTHOR = {"login": "author", "type": "User"}
REVIEWER = {"login": "reviewer", "type": "User"}
BOT = {"login": "dependabot[bot]", "type": "Bot"}
BASE_SHA, REPLAY_SHA, LATER_SHA, MERGE_BASE_SHA = "b" * 40, "c" * 40, "d" * 40, "e" * 40
OLD = "def total(items):\n    return sum(items)\n"
NEW = OLD + "\n\ndef average(items):\n    return total(items) / len(items)\n"


def pull(number=12, user=AUTHOR, title="Fix the session cookie path", merged=True, created="2026"):
    return {
        "number": number,
        "title": title,
        "user": user,
        "created_at": f"{created}-06-01T10:00:00Z",
        "merged_at": "2026-06-03T10:00:00Z" if merged else None,
        "base": {"sha": BASE_SHA},
    }


def review(user=REVIEWER, at="2026-06-02T09:00:00Z", commit=REPLAY_SHA):
    return {"user": user, "submitted_at": at, "commit_id": commit}


def changed(filename="starlette/sessions.py", additions=10, deletions=2, patch=None, **fields):
    patch = make_patch(OLD, NEW) if patch is None else patch  # "" means GitHub gave no patch
    return {
        "filename": filename,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
        "patch": patch,
        **fields,
    }


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (pull(), None),
        (pull(title="Fix version parsing in the client"), None),
        (pull(merged=False), "not merged"),
        (pull(user=BOT), "opened by a bot"),
        (pull(user=None), "opened by a bot"),  # a deleted account
        (pull(title="Version 0.40.0"), "release or version bump"),
        (pull(title="Bump httpcore to 1.0"), "release or version bump"),
        (pull(title="Prepare release 2.3"), "release or version bump"),
    ],
)
def test_the_rules_that_need_only_the_pr(candidate, reason):
    assert pull_reject_reason(candidate) == reason


def test_the_first_human_review_is_the_earliest_by_someone_else():
    reviews = [
        review(at="2026-06-02T12:00:00Z", commit=LATER_SHA),
        review(at="2026-06-02T09:00:00Z", commit=REPLAY_SHA),
        # None of these is a reviewer's review, though each is earlier:
        review(user=AUTHOR, at="2026-06-01T11:00:00Z", commit=BASE_SHA),  # a reply by the author
        review(user=BOT, at="2026-06-01T11:00:00Z", commit=BASE_SHA),
        review(user=None, at="2026-06-01T11:00:00Z", commit=BASE_SHA),  # a deleted account
        {"user": REVIEWER, "commit_id": BASE_SHA},  # pending: never submitted
    ]

    assert first_human_review(pull(), reviews)["commit_id"] == REPLAY_SHA
    assert first_human_review(pull(), reviews[2:]) is None


def test_the_rules_about_the_review():
    commits = [REPLAY_SHA, LATER_SHA]
    by_reviewer = [{"user": REVIEWER}]

    assert review_reject_reason(pull(), [review()], by_reviewer, commits) is None
    # The author answering their own PR, or a bot, isn't a reviewer's line comment.
    own = [{"user": AUTHOR}, {"user": BOT}, {"user": None}]
    assert review_reject_reason(pull(), [review()], own, commits) == (
        "no line comment from a reviewer"
    )
    assert review_reject_reason(pull(), [], by_reviewer, commits) == (
        "no review by a person other than the author"
    )
    # The reviewed commit isn't among the PR's commits any more.
    assert review_reject_reason(pull(), [review()], by_reviewer, [LATER_SHA]) == (
        "the first review's commit was force-pushed away"
    )


def test_the_rules_about_the_replayed_diff():
    assert diff_reject_reason([changed()]) is None
    assert diff_reject_reason([changed()] * (MAX_FILES + 1)) == "more than 10 files"
    # Added and deleted lines both count.
    half = MAX_LINES // 2
    assert diff_reject_reason([changed(additions=half, deletions=half)]) is None
    assert diff_reject_reason([changed(additions=half + 1, deletions=half)]) == (
        "more than 300 changed lines"
    )
    not_source = [
        changed("tests/test_sessions.py"),
        changed("docs/sessions.md"),
        changed("docs/conf.py"),
        changed("starlette/old.py", status="removed"),
    ]
    assert diff_reject_reason(not_source) == "no .py file outside tests and docs"


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("# see https://github.com/Kludex/starlette/issues/2501", LINKED),
        ("# see https://github.com/encode/starlette/pull/2501", LINKED),  # the repo's old name
        ("# fixed in encode/httpx#2501", LINKED),  # another project's issue counts too
        ("# see https://github.com/Kludex/starlette/blob/main/README.md", None),  # not an issue
        ("# TODO(#12): tidy this up", None),  # a bare number names no repo
    ],
)
def test_a_diff_that_links_to_a_github_issue_is_turned_away(line, reason):
    patch = make_patch(OLD, OLD + line + "\n")

    assert diff_reject_reason([changed(patch=patch)]) == reason
    # A file GitHub gives no patch for (binary) has nothing to search.
    assert diff_reject_reason([changed(), changed("logo.png", patch="")]) is None


def test_the_sample_is_reproducible_and_the_reserve_is_disjoint():
    entries = [{"number": n} for n in range(1, 36)]

    sample, reserve = draw(entries, seed=1, repo=REPO)

    assert (len(sample), len(reserve)) == (PER_REPO, PER_REPO)
    assert not {e["number"] for e in sample} & {e["number"] for e in reserve}
    # The same seed gives the same draw, whatever order GitHub listed the PRs in.
    assert draw(list(reversed(entries)), seed=1, repo=REPO) == (sample, reserve)
    assert draw(entries, seed=2, repo=REPO) != (sample, reserve)
    assert draw(entries, seed=1, repo="encode/httpx") != (sample, reserve)
    # It's a shuffle, not the first ten.
    assert sample != entries[:PER_REPO]


def test_a_repo_with_too_few_prs_fills_the_sample_first():
    sample, reserve = draw([{"number": n} for n in range(PER_REPO + 4)], seed=1, repo=REPO)

    assert (len(sample), len(reserve)) == (PER_REPO, 4)


def test_the_sample_takes_a_pr_from_each_repo_in_turn():
    a1, a2, b1, c1, c2 = ({"number": n} for n in range(5))

    mixed = interleave([[a1, a2], [b1], [c1, c2]])

    assert [(e["k"], e["number"]) for e in mixed] == [(1, 0), (2, 2), (3, 3), (4, 1), (5, 4)]


def test_the_estimate_counts_every_lane_of_every_reviewed_chunk():
    file = changed("starlette/sessions.py")
    other = changed("starlette/routing.py")
    contents = {"starlette/sessions.py": NEW, "starlette/routing.py": NEW}
    [chunk] = build_chunks("starlette/sessions.py", parse_patch(file["patch"]), NEW)
    one_file = sum(estimate_call_tokens(lane, chunk) for lane in LANES)

    assert lane_tokens([file], contents) == one_file
    assert lane_tokens([file, other], contents) > one_file
    # A file that isn't reviewed (so has no contents) costs nothing.
    assert lane_tokens([file, changed("uv.lock")], contents) == one_file


def test_contents_are_fetched_only_for_files_the_job_reviews(github_api):
    route = github_api.get(f"/repos/{REPO}/contents/starlette/sessions.py").mock(
        return_value=httpx.Response(200, text=NEW)
    )
    files = [
        changed("starlette/sessions.py"),
        changed("starlette/old.py", status="removed"),
        changed("uv.lock"),
        changed("logo.png", patch=""),
        changed("starlette/gone.py", patch="@@ -1,2 +0,0 @@\n-a\n-b"),  # only deletions
    ]

    with github_client("token") as http:
        contents = fetch_contents(http, REPO, REPLAY_SHA, files)

    assert contents == {"starlette/sessions.py": NEW}
    request = route.calls.last.request
    assert request.url.params["ref"] == REPLAY_SHA
    assert request.headers["Accept"] == "application/vnd.github.raw+json"
    assert request.headers["Authorization"] == "Bearer token"


def test_qualifying_prs_are_found_through_the_api(github_api):
    def answers(url, items):
        return github_api.get(url).mock(return_value=httpx.Response(200, json=items))

    pulls = f"/repos/{REPO}/pulls"
    # Newest first. #10 is older than the window, so the listing stops there.
    answers(pulls, [pull(12), pull(11, user=BOT), pull(10, created="2020"), pull(9)])
    answers(f"{pulls}/12/reviews", [review()])
    answers(f"{pulls}/12/comments", [{"user": REVIEWER}])
    answers(f"{pulls}/12/commits", [{"sha": REPLAY_SHA}, {"sha": LATER_SHA}])
    compare = answers(
        f"/repos/{REPO}/compare/{BASE_SHA}...{REPLAY_SHA}",
        {"merge_base_commit": {"sha": MERGE_BASE_SHA}, "files": [changed()]},
    )

    with github_client("token") as http:
        entries, files, rejected = find_qualifying(http, REPO, datetime(2024, 11, 11, tzinfo=UTC))

    assert entries == [
        {
            "repo": REPO,
            "number": 12,
            "replay_sha": REPLAY_SHA,
            "merge_base_sha": MERGE_BASE_SHA,
            "files": 1,
            "lines": 12,
        }
    ]
    assert files == {12: [changed()]}
    # The bot's PR cost no further requests (an unmocked one would fail the test), and nothing
    # at or after #10 was looked at.
    assert rejected == {"opened by a bot": 1}
    assert compare.call_count == 1


def test_a_timeout_is_tried_again_and_an_http_error_is_not(github_api, monkeypatch):
    monkeypatch.setattr(common, "GET_RETRY_SECONDS", 0)
    slow = github_api.get("/slow").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.ConnectError("dropped"), httpx.Response(200)]
    )
    down = github_api.get("/down").mock(side_effect=httpx.ReadTimeout("slow"))
    missing = github_api.get("/missing").mock(return_value=httpx.Response(404))

    with github_client("token") as http:
        assert common.get(http, "/slow").status_code == 200
        with pytest.raises(httpx.ReadTimeout):
            common.get(http, "/down")
        with pytest.raises(httpx.HTTPStatusError):
            common.get(http, "/missing")

    assert (slow.call_count, down.call_count, missing.call_count) == (3, common.GET_TRIES, 1)
