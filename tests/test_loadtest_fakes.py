"""The load test's fakes, through the real GitHubClient and GroqClient wired exactly as the
load-test worker wires them (URL rewriting included), in-process via ASGITransport."""

import asyncio
import io
import tarfile
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.github.diff import parse_patch
from app.github.http import RetryableError
from app.github.webhooks import PullRequestPayload
from app.llm.groq import LLMError
from app.pipeline.chunking import build_chunks
from app.pipeline.lanes import run_lane
from loadtest.fakes import create_app, pr_files
from loadtest.worker import FakesTransport, fake_clients

FAKES_URL = "http://fakes:9000"
REPO = "load-org-1/repo-1"
PULL = {"installation_id": 1, "repo_id": 101, "repo": REPO, "number": 7, "sha": "a" * 40}


@pytest.fixture
def fakes_app():
    return create_app()


@pytest.fixture
async def admin(fakes_app):
    transport = httpx.ASGITransport(app=fakes_app)
    async with httpx.AsyncClient(transport=transport, base_url=FAKES_URL) as client:
        (await client.post("/_admin/pulls", json=PULL)).raise_for_status()
        yield client


@pytest.fixture
async def clients(fakes_app):
    github, groq = fake_clients(FAKES_URL, "worker-1", httpx.ASGITransport(app=fakes_app))
    yield github, groq
    await github.aclose()
    await groq.aclose()


async def set_config(admin, **config) -> None:
    (await admin.put("/_admin/config", json=config)).raise_for_status()


async def records(admin) -> dict:
    return (await admin.get("/_admin/records")).json()


async def test_job_endpoints_answer_what_the_job_expects(admin, clients):
    github, _ = clients
    sha = PULL["sha"]

    pull = await github.get_pull(1, REPO, 7)
    assert (pull["state"], pull["draft"], pull["head"]["sha"]) == ("open", False, sha)
    [file] = await github.list_pull_files(1, REPO, 7)
    hunks = parse_patch(file["patch"])
    assert hunks and hunks[0].added
    content = await github.get_file_content(1, REPO, file["filename"], sha)
    assert content == pr_files(7, sha)[1][file["filename"]]
    assert await github.list_check_runs(1, REPO, sha) == []

    # The tarball comes through GitHub's redirect, with one top folder like GitHub's.
    data = b"".join([piece async for piece in github.iter_tarball(1, REPO, sha)])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert [name.split("/", 1)[1] for name in names] == [file["filename"]]

    # Posted reviews are listed as our bot's, so the job's marker check finds them.
    await github.create_review(1, REPO, 7, sha, "body <!-- marker -->", [])
    bot = await github.bot_login()
    [review] = await github.list_reviews(1, REPO, 7)
    assert review["user"]["login"] == bot == "loadtest[bot]"
    assert "<!-- marker -->" in review["body"]


async def test_reconciler_endpoints_list_the_open_pull(admin, clients):
    github, _ = clients

    [installation] = await github.list_installations()
    [repo] = await github.list_installation_repos(installation["id"])
    pulls = await github.list_open_pulls(1, REPO, datetime.now(UTC) - timedelta(hours=1))

    assert installation == {"id": 1, "suspended_at": None}
    assert [p["number"] for p in pulls] == [7]
    # The same payload shape the reconciler builds and enqueues.
    payload = {"action": "reconcile", "installation": installation, "repository": repo}
    PullRequestPayload.model_validate(payload | {"pull_request": pulls[0]})


async def test_a_push_moves_the_head_and_the_first_content_request_marks_the_job(admin, clients):
    github, _ = clients
    new_sha = "b" * 40
    (await admin.post("/_admin/pulls", json=PULL | {"sha": new_sha})).raise_for_status()

    pull = await github.get_pull(1, REPO, 7)
    await github.get_file_content(1, REPO, "app/feature_7.py", new_sha)

    assert pull["head"]["sha"] == new_sha
    seen = (await records(admin))["first_seen"]
    assert list(seen) == [new_sha]
    assert seen[new_sha]["worker"] == "worker-1"


async def test_lanes_get_valid_output_and_only_correctness_reports(admin, clients):
    _, groq = clients
    path = "app/feature_7.py"
    head = pr_files(7, PULL["sha"])[1]
    hunks = parse_patch((await clients[0].list_pull_files(1, REPO, 7))[0]["patch"])
    [chunk] = build_chunks(path, hunks, head[path])
    deadline = time.monotonic() + 30

    correctness = await run_lane(groq, "m", "correctness", chunk, deadline)
    security = await run_lane(groq, "m", "security", chunk, deadline)

    [finding] = correctness.candidates
    assert finding.line_start == max(hunks[0].added)  # the division, the last added line
    assert security.candidates == []


async def test_github_fault_is_retryable_and_only_inside_its_window(admin, clients):
    github, _ = clients
    later = time.time() + 3600
    await set_config(admin, github_fault={"status": 502, "rate": 1.0, "start": later})
    await github.get_pull(1, REPO, 7)  # the window hasn't opened

    await set_config(admin, github_fault={"status": 502, "rate": 1.0})
    with pytest.raises(RetryableError):
        await github.get_pull(1, REPO, 7)
    assert (await records(admin))["github_errors"] == {"502": 1}


async def test_review_stored_then_failed_is_found_by_the_retry(admin, clients):
    github, _ = clients
    await set_config(admin, review_fail_status=502)

    with pytest.raises(RetryableError):
        await github.create_review(1, REPO, 7, PULL["sha"], "<!-- run:1 -->", [])

    [review] = await github.list_reviews(1, REPO, 7)
    assert "<!-- run:1 -->" in review["body"]


async def test_held_review_is_stored_before_the_answer(admin, clients):
    github, _ = clients
    await set_config(admin, review_hold_seconds=0.5)

    post = asyncio.create_task(github.create_review(1, REPO, 7, PULL["sha"], "body", []))
    # Wait for the review to be stored instead of guessing how long that takes: the token
    # request before it alone took about 0.1 s.
    stored = False
    for _ in range(500):  # poll for up to 5 s
        stored = bool((await records(admin))["reviews"])
        if stored:
            break
        await asyncio.sleep(0.01)
    held = not post.done()
    await post

    assert stored and held  # stored, and the answer still held back


async def test_rate_limit_headers_make_the_client_defer(admin, clients):
    github, _ = clients
    await set_config(admin, github_rate_limit=50)

    await github.get_pull(1, REPO, 7)  # 49 left: below the client's minimum of 50
    with pytest.raises(RetryableError) as exc:
        await github.get_pull(1, REPO, 7)
    assert exc.value.retry_after > 3000  # until the hourly window resets


async def test_groq_429s_are_retried_then_the_call_fails(admin, clients):
    _, groq = clients
    await set_config(admin, groq_fault={"status": 429, "rate": 1.0, "retry_after": 0.01})
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    schema = {"name": "correctness_findings"}

    with pytest.raises(LLMError):
        await groq.complete_json(
            "m", messages, "correctness_findings", schema, time.monotonic() + 30
        )

    assert (await records(admin))["groq_calls"] == {"lane 429": 3}


async def test_tokens_per_minute_limit_answers_like_groq(admin):
    await set_config(admin, groq_tpm=500)  # each call below counts as about 310 tokens
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "response_format": {"json_schema": {"name": "security_findings"}},
    }

    first = await admin.post("/openai/v1/chat/completions", json=body)
    second = await admin.post("/openai/v1/chat/completions", json=body)

    assert first.status_code == 200
    assert 0 < int(first.headers["x-ratelimit-remaining-tokens"]) < 500
    assert second.status_code == 429
    assert float(second.headers["retry-after"]) > 0


def test_transport_rewrites_known_hosts_and_refuses_others():
    seen = []

    def inner_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    transport = FakesTransport(FAKES_URL, httpx.MockTransport(inner_handler))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post("https://api.groq.com/openai/v1/chat/completions")
            with pytest.raises(RuntimeError):
                await client.get("https://example.com/")

    asyncio.run(go())
    assert seen == [f"{FAKES_URL}/openai/v1/chat/completions"]
