import json
import time

import httpx
import pytest

from app.github.http import NonRetryableError, RetryableError
from tests.factories import INSTALLATION_ID, PR_NUMBER, REPO, pull_response

PULL_URL = f"/repos/{REPO}/pulls/{PR_NUMBER}"
FILES_URL = f"{PULL_URL}/files"
REVIEWS_URL = f"{PULL_URL}/reviews"


def rate_limit_headers(remaining: int, reset_in: float = 600) -> dict:
    return {
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(time.time() + reset_in)),
    }


async def test_get_pull_sends_installation_token(github, github_api):
    route = github_api.get(PULL_URL).mock(return_value=httpx.Response(200, json=pull_response()))

    pull = await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)

    assert pull["state"] == "open"
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-token"


async def test_list_pull_files_follows_pagination(github, github_api):
    next_page = f"https://api.github.com{FILES_URL}?per_page=100&page=2"
    github_api.get(FILES_URL, params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[{"filename": "c.py"}])
    )
    github_api.get(FILES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[{"filename": "a.py"}, {"filename": "b.py"}],
            headers={"Link": f'<{next_page}>; rel="next"'},
        )
    )

    files = await github.list_pull_files(INSTALLATION_ID, REPO, PR_NUMBER)

    assert [f["filename"] for f in files] == ["a.py", "b.py", "c.py"]


async def test_get_file_content_asks_for_raw_text_at_the_commit(github, github_api):
    route = github_api.get(f"/repos/{REPO}/contents/src/my%20file.py").mock(
        return_value=httpx.Response(200, text="print('hi')\n")
    )

    text = await github.get_file_content(INSTALLATION_ID, REPO, "src/my file.py", "abc123")

    assert text == "print('hi')\n"
    request = route.calls.last.request
    assert request.headers["Accept"] == "application/vnd.github.raw+json"
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.url.params["ref"] == "abc123"


async def test_create_review_posts_a_comment_review(github, github_api):
    route = github_api.post(REVIEWS_URL).mock(return_value=httpx.Response(200, json={"id": 9}))
    comments = [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "x"}]

    review = await github.create_review(INSTALLATION_ID, REPO, PR_NUMBER, "abc", "sum", comments)

    assert review["id"] == 9
    assert json.loads(route.calls.last.request.content) == {
        "commit_id": "abc",
        "event": "COMMENT",
        "body": "sum",
        "comments": comments,
    }


async def test_bot_login_comes_from_the_app_slug_and_is_cached(github, github_api):
    route = github_api.get("/app").mock(
        return_value=httpx.Response(200, json={"slug": "my-reviewer"})
    )

    assert await github.bot_login() == "my-reviewer[bot]"
    assert await github.bot_login() == "my-reviewer[bot]"
    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-jwt"


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_retryable(github, github_api, status):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(RetryableError):
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)


async def test_timeouts_are_retryable(github, github_api):
    github_api.get(PULL_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(RetryableError):
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)


@pytest.mark.parametrize("status", [404, 422])
async def test_client_errors_are_not_retryable(github, github_api, status):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(status, json={"message": "nope"}))

    with pytest.raises(NonRetryableError):
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)


async def test_403_without_rate_limit_is_a_permission_error(github, github_api):
    github_api.get(PULL_URL).mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible"})
    )

    with pytest.raises(NonRetryableError):
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)


async def test_secondary_rate_limit_uses_retry_after(github, github_api):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(403, headers={"Retry-After": "30"}))

    with pytest.raises(RetryableError) as exc_info:
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)

    assert exc_info.value.retry_after == 30


async def test_primary_rate_limit_waits_until_reset(github, github_api):
    github_api.get(PULL_URL).mock(
        return_value=httpx.Response(403, headers=rate_limit_headers(0, reset_in=120))
    )

    with pytest.raises(RetryableError) as exc_info:
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)

    assert 110 < exc_info.value.retry_after <= 120


async def test_429_is_retryable(github, github_api):
    github_api.get(PULL_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RetryableError):
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)


async def test_backs_off_before_using_up_the_rate_limit(github, github_api):
    route = github_api.get(PULL_URL).mock(
        return_value=httpx.Response(200, json=pull_response(), headers=rate_limit_headers(10))
    )

    await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)  # learns only 10 requests remain
    with pytest.raises(RetryableError) as exc_info:
        await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)

    assert route.call_count == 1  # the second call never reached GitHub
    assert exc_info.value.retry_after > 500


async def test_rate_limit_is_tracked_per_installation(github, github_api):
    github_api.get(PULL_URL).mock(
        return_value=httpx.Response(200, json=pull_response(), headers=rate_limit_headers(10))
    )

    await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)
    # A different installation has its own limit, so it isn't blocked.
    await github.get_pull(INSTALLATION_ID + 1, REPO, PR_NUMBER)


async def test_requests_resume_after_the_limit_resets(github, github_api):
    route = github_api.get(PULL_URL).mock(
        return_value=httpx.Response(
            200, json=pull_response(), headers=rate_limit_headers(10, reset_in=-1)
        )
    )

    await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)
    await github.get_pull(INSTALLATION_ID, REPO, PR_NUMBER)  # reset time already passed

    assert route.call_count == 2
