"""The real-world report, from small hand-built results with hand-computed statistics."""

from realworld.report import render

SAMPLE = [{"k": k, "repo": repo} for k, repo in enumerate(["o/a", "o/b", "o/a", "o/b"], start=1)]


def comment(comment_id: int, file: str = "a.py", line: int = 3) -> dict:
    body = f"**high** · bug · Finding {comment_id}\n\nWhy."
    return {"id": comment_id, "url": f"https://x/{comment_id}", "file": file, "line": line} | {
        "start_line": line,
        "body": body,
    }


def result(k: int, repo: str, comments: list[dict], **fields) -> dict:
    verdicts = ["confirmed"] * len(comments) + ["dropped"]
    return {
        "k": k,
        "upstream": {"repo": repo},
        "budget_limited": False,
        "problems": [],
        "notes": [],
        "comments": comments,
        "pr_created_at": "2026-11-11T10:00:00Z",
        "review": {"submitted_at": "2026-11-11T10:04:00Z"},
        "run": {"cost_usd": "0.004000000"},
        "findings": [{"verdict": verdict, "drop_reason": None} for verdict in verdicts]
        + [{"verdict": None, "drop_reason": "duplicate"}],
        **fields,
    }


RESULTS = [
    result(
        1, "o/a", [comment(11), comment(12)], notes=["2 lane call(s) not run (PR token budget)"]
    ),
    result(2, "o/b", [comment(21), comment(22)], run={"cost_usd": "0.008000000"}),
    result(3, "o/a", []),
    # Cut short by the daily budget: listed, and left out of every number below.
    result(4, "o/b", [comment(41)], budget_limited=True, problems=["finding 9 not verified"]),
]
LABELS = [
    {"comment_id": "11", "label": "real"},
    {"comment_id": "12", "label": "wrong"},
    {"comment_id": "21", "label": "Real "},
    {"comment_id": "22", "label": ""},  # not labelled yet
    {"comment_id": "41", "label": "real"},
]


def outcome(added, added_changed, flagged, flagged_changed, comments) -> dict:
    return {"added": added, "added_changed": added_changed, "flagged": flagged} | {
        "flagged_changed": flagged_changed,
        "comments": {
            str(comment_id): {"changed_before_merge": changed, "reviewer_comment_ids": near}
            for comment_id, changed, near in comments
        },
    }


# In the scored PRs: 10 added lines, 3 flagged and all 3 changed; 1 of the other 7 changed.
OUTCOMES = {
    "1": outcome(6, 3, 2, 2, [(11, True, [71]), (12, True, [])]),
    "2": outcome(4, 1, 1, 1, [(21, True, []), (22, False, [])]),
    "3": outcome(0, 0, 0, 0, []),
    "4": outcome(50, 50, 50, 50, [(41, True, [99])]),
}


def reviewer(k, comment_id, problem, bot_comment_url="", same_issue="") -> dict:
    return {"k": str(k), "comment_id": str(comment_id), "problem": problem} | {
        "bot_comment_url": bot_comment_url,
        "same_issue": same_issue,
    }


REVIEWERS = [
    reviewer(1, 71, "yes", "https://x/11", "yes"),  # a problem the bot also flagged
    reviewer(1, 72, "yes"),  # a problem the bot missed
    reviewer(2, 73, "no"),  # a style remark
    reviewer(2, 74, ""),  # not marked yet
    reviewer(4, 99, "yes", "https://x/41", "yes"),  # in the budget-limited PR
]


def report(**changes) -> str:
    inputs = {"results": RESULTS, "labels": LABELS, "outcomes": OUTCOMES} | changes
    return render(SAMPLE, dict.fromkeys("1234"), reviewer_rows=REVIEWERS, **inputs)


def test_coverage_names_the_budget_limited_review_and_leaves_it_out():
    text = report()

    assert "- Replayed: 4 of 4 sampled PRs; collected: 4." in text
    assert (
        "- Scored: 3." in text and "cut the review short, or the run didn't complete): 1." in text
    )
    assert "  - Replay 4: finding 9 not verified" in text
    assert "https://x/41" not in text  # its comment appears nowhere


def test_precision_counts_only_labelled_comments_per_repo_and_in_total():
    text = report()

    assert "**1 comment(s) aren't labelled yet** and aren't counted." in text
    # Repo, PRs, PRs with no comment, comments, real, wrong, nit, precision, wrong rate.
    assert "| o/a | 2 | 1 | 2 | 1 | 1 | 0 | 1/2: 9–91% | 1/2: 9–91% |" in text
    assert "| o/b | 1 | 0 | 2 | 1 | 0 | 0 | 1/1: 21–100% | 0/1: 0–79% |" in text
    # Wilson, by hand: 2 of 3 is 21–94%.
    assert "| All | 3 | 1 | 4 | 2 | 1 | 0 | 2/3: 21–94% | 1/3: 6–79% |" in text


def test_agreement_with_reviewers_in_both_directions():
    text = report()

    assert "version both saw): 1/4: 5–70%. Checked by hand to be about the same issue: 1." in text
    assert "| Flagged by the bot | 3/3: 44–100% |" in text
    assert "| Not flagged (the base rate) | 1/7: 3–51% |" in text
    # By hand: all 3 flagged lines among the 4 changed, of 10: C(4,3) / C(10,3) = 4/120.
    assert "**p = 0.033** (Fisher's exact test, one-sided)" in text
    assert (
        "left 4 line comment(s) on the replayed versions; 2 are marked as a real code problem"
        in text
    )
    assert "; 1 aren't marked yet. The bot commented within 3 lines of 1/2: 9–91% of those" in text
    assert "and 1 of the matches are confirmed by hand" in text


def test_production_numbers_and_the_comment_table():
    text = report()

    assert "$0.0053 on average, $0.0080 at most" in text
    assert "4.0 minutes (median), 4.0 at most" in text
    assert "**The agent's verdicts on the candidates:** 4 confirmed, 3 dropped." in text
    assert "per-PR token budget or the time limit:** 1 of 3." in text
    assert "| 1 | [`a.py:3`](https://x/11) | high · bug · Finding 11 | real | yes | yes |" in text
    assert "| 2 | [`a.py:3`](https://x/22) | high · bug · Finding 22 | — | no | no |" in text


def test_the_report_renders_before_outcomes_and_labels_exist():
    text = report(labels=[], outcomes={})

    assert "**4 comment(s) aren't labelled yet**" in text
    assert "| All | 3 | 1 | 4 | 0 | 0 | 0 | n/a | n/a |" in text
    assert "No outcomes yet: run `python -m realworld.outcomes`." in text
    assert "| 1 | [`a.py:3`](https://x/11) | high · bug · Finding 11 | — | — | — |" in text
