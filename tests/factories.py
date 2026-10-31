"""Builders for test data shaped like GitHub's."""

SHA_1 = "a" * 40
SHA_2 = "b" * 40
REPO = "octo/hello"
PR_NUMBER = 7
INSTALLATION_ID = 555


def pr_payload(
    action: str = "opened", sha: str = SHA_1, draft: bool = False, title: str = "Add feature"
) -> dict:
    """A pull_request webhook payload, trimmed to the fields we read plus a few we ignore."""
    return {
        "action": action,
        "number": PR_NUMBER,
        "installation": {"id": INSTALLATION_ID, "node_id": "ignored"},
        "repository": {
            "id": 1001,
            "full_name": REPO,
            "private": False,
            "owner": {"login": "octo", "id": 1},
        },
        "pull_request": {
            "number": PR_NUMBER,
            "title": title,
            "draft": draft,
            "state": "open",
            "head": {"sha": sha, "ref": "feature"},
            "user": {"login": "alice"},
        },
        "sender": {"login": "alice"},
    }


def pull_response(sha: str = SHA_1, state: str = "open", draft: bool = False) -> dict:
    """GitHub's GET /repos/{repo}/pulls/{n} response, trimmed."""
    return {"number": PR_NUMBER, "state": state, "draft": draft, "head": {"sha": sha}}
