"""B's raw material: which lines changed before the merge, and which reviewer comments sit
near a bot comment."""

import pytest

from realworld.outcomes import diff_lines, near, pull_outcome, reviewer_comments
from realworld.replay import REPLAY_IDENTITY, git

REPLAY_SHA = "c" * 40
AUTHOR = {"login": "author", "type": "User"}
REVIEWER = {"login": "reviewer", "type": "User"}


@pytest.fixture
def repo(tmp_path):
    """A git repo and a function that commits a whole set of files and returns the commit."""
    git(tmp_path, "init", "--quiet")

    def commit(files: dict[str, str]) -> str:
        for old in tmp_path.glob("*.py"):
            old.unlink()
        for name, text in files.items():
            (tmp_path / name).write_text(text, encoding="utf-8")
        git(tmp_path, "add", "--all")
        quiet = ("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "c")
        git(tmp_path, *quiet, env=REPLAY_IDENTITY)
        return git(tmp_path, "rev-parse", "HEAD")

    return tmp_path, commit


def numbered(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def test_changed_lines_are_in_the_old_versions_numbers_and_moved_lines_do_not_count(repo):
    work, commit = repo
    replayed = commit({"a.py": numbered("one", "two", "three", "four", "five")})
    # Two lines go in above (everything below moves down), "three" is edited, "five" removed.
    merged = commit({"a.py": numbered("new", "new", "one", "two", "THREE", "four")})

    assert diff_lines(work, replayed, merged, "old") == {"a.py": {3, 5}}
    assert diff_lines(work, replayed, merged, "new") == {"a.py": {1, 2, 5}}


def test_a_renamed_file_is_found_by_its_old_name(repo):
    work, commit = repo
    body = [f"line {n}" for n in range(1, 21)]
    replayed = commit({"old_name.py": numbered(*body)})
    body[9] = "line 10, edited"
    merged = commit({"new_name.py": numbered(*body)})

    assert diff_lines(work, replayed, merged, "old") == {"old_name.py": {10}}
    assert diff_lines(work, replayed, merged, "new") == {"new_name.py": {10}}


def test_added_and_deleted_files_and_lines_that_look_like_diff_headers(repo):
    work, commit = repo
    base = commit(
        {"gone.py": numbered("a", "b"), "sql.py": numbered("x", "-- a/other.py", "y", "z")}
    )
    head = commit({"added.py": numbered("p", "q", "r"), "sql.py": numbered("x", "y", "++ new")})

    # The removed line "-- a/other.py" shows in the diff as "--- a/other.py". It's content, not
    # a file header: the hunk after it (line 4) still belongs to sql.py.
    assert diff_lines(work, base, head, "old") == {"gone.py": {1, 2}, "sql.py": {2, 4}}
    assert diff_lines(work, base, head, "new") == {"added.py": {1, 2, 3}, "sql.py": {3}}


def test_a_diff_with_text_outside_the_windows_code_page_is_read(repo):
    # Replay 7 (Starlette) crashed here: "”" is the bytes E2 80 9D in UTF-8, and Windows
    # decoded git's output as cp1252, which has no character for 9D.
    work, commit = repo
    replayed = commit({"a.py": numbered("x = 1", 'msg = "“quoted”"', "y = 2")})
    merged = commit({"a.py": numbered("x = 1", 'msg = "“quoted, edited”"', "y = 2")})

    assert diff_lines(work, replayed, merged, "old") == {"a.py": {2}}


def github_comment(comment_id=1, user=REVIEWER, line=10, start=None, **fields) -> dict:
    return {
        "id": comment_id,
        "html_url": f"https://x/{comment_id}",
        "user": user,
        "path": "a.py",
        "body": "This  can be\nNone here.",
        "original_commit_id": REPLAY_SHA,
        "original_line": line,
        "original_start_line": start,
        "side": "RIGHT",
        **fields,
    }


def test_only_reviewers_own_comments_on_the_replayed_version_count():
    comments = [
        github_comment(1, line=10, start=8),
        github_comment(2, user=AUTHOR),  # the author answering
        github_comment(3, user={"login": "cubic[bot]", "type": "Bot"}),  # a review bot
        github_comment(4, original_commit_id="d" * 40),  # made on a later version
        github_comment(5, in_reply_to_id=1),  # a reply in a thread
        github_comment(6, side="LEFT"),  # on a deleted line
        github_comment(7, user=None),  # a deleted account
    ]

    [kept] = reviewer_comments(comments, "author", REPLAY_SHA)

    assert kept == {"id": 1, "url": "https://x/1", "file": "a.py", "line": 10, "start_line": 8} | {
        "text": "This can be None here."
    }


def place(file="a.py", start=10, line=10, **fields) -> dict:
    return {"file": file, "start_line": start, "line": line, **fields}


@pytest.mark.parametrize(
    ("bot", "reviewer", "expected"),
    [
        (place(), place(), True),
        (place(), place(start=13, line=13), True),  # 3 lines below
        (place(), place(start=14, line=14), False),  # 4 lines below
        (place(), place(start=7, line=7), True),  # 3 lines above
        (place(), place(start=6, line=6), False),
        (place(start=10, line=12), place(start=15, line=20), True),  # ranges: 12 and 15
        (place(start=10, line=12), place(start=16, line=20), False),
        (place(), place(file="b.py"), False),
    ],
)
def test_two_comments_match_within_three_lines_in_the_same_file(bot, reviewer, expected):
    assert near(bot, reviewer) is expected
    assert near(reviewer, bot) is expected


def test_a_pulls_outcome_counts_lines_and_pairs_comments():
    bot_changed = place(start=10, line=11, id=1, url="bot-1")  # line 11 changes before merge
    bot_kept = place(file="b.py", start=5, line=5, id=2, url="bot-2")
    on_context = place(start=30, line=30, id=3, url="bot-3")  # a line this PR didn't add
    result = {"k": 4, "comments": [bot_changed, bot_kept, on_context]}
    added = {"a.py": {10, 11, 12, 13}, "b.py": {5, 6}}
    changed = {"a.py": {11, 12, 99}, "b.py": set()}  # 99 wasn't added by this PR
    reviewers = [
        place(start=13, line=13, id=71, url="rev-71", text="near the first"),
        place(file="b.py", start=40, line=40, id=72, url="rev-72", text="near nothing"),
    ]

    outcome, rows = pull_outcome(result, added, changed, reviewers)

    assert (outcome["added"], outcome["added_changed"]) == (6, 2)
    assert (outcome["flagged"], outcome["flagged_changed"]) == (3, 1)
    assert outcome["comments"] == {
        "1": {"changed_before_merge": True, "reviewer_comment_ids": [71]},
        "2": {"changed_before_merge": False, "reviewer_comment_ids": []},
        "3": {"changed_before_merge": False, "reviewer_comment_ids": []},
    }
    assert [
        (r["comment_id"], r["bot_comment_url"], r["problem"], r["same_issue"]) for r in rows
    ] == [
        (71, "bot-1", "", ""),
        (72, "", "", ""),
    ]
    assert rows[0] | {"k": 4, "file": "a.py", "line": 13, "text": "near the first"} == rows[0]
