"""Fake GitHub and fake Groq for the load test, as one HTTP server.

Workers run in separate processes (and get killed), so the fakes are a server rather than
objects in the job's process: every worker sees the same PRs, and every review posted lands in
one place, where duplicates can be counted. The real GitHubClient and GroqClient talk to it, so
their error handling (5xx, 429, rate-limit headers) is part of what's tested.

The load generator drives it through /_admin: register PRs and pushes, set latency and faults,
read what happened.

    uvicorn loadtest.fakes:app --port 9000
"""

import asyncio
import json
import random
import re
import time
from collections import Counter
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel

from evals.fake_github import pull_files, tarball

BOT_SLUG = "loadtest"
TPM_WINDOW_SECONDS = 60
# Groq bills the request; this is close enough to decide when a minute is used up.
TOOL_CALL_OUTPUT_TOKENS = 300

BASE_CODE = "def existing(values):\n    return sorted(values)\n"


def pr_files(number: int, sha: str) -> tuple[dict[str, str], dict[str, str]]:
    """Base and head files of a PR. The added code differs per head, so no two pushes share
    a lane cache entry and every review does the full work."""
    path = f"app/feature_{number}.py"
    added = (
        f"\n\ndef average_{sha[:8]}(items):\n"
        "    total = sum(items)\n"
        "    return total / len(items)\n"
    )
    base = {path: BASE_CODE}
    return base, {path: BASE_CODE + added}


class Fault(BaseModel):
    """Answer `status` to this share of calls while the window is open (unix times, None
    means unbounded). `retry_after` is sent as the Retry-After header."""

    status: int = 503
    rate: float = 0.0
    start: float | None = None
    end: float | None = None
    retry_after: float | None = None

    def hits(self, now: float) -> bool:
        if self.start is not None and now < self.start:
            return False
        if self.end is not None and now >= self.end:
            return False
        return random.random() < self.rate

    def response(self) -> JSONResponse:
        headers = {"retry-after": str(self.retry_after)} if self.retry_after is not None else {}
        return JSONResponse({"message": "load test fault"}, self.status, headers=headers)


class Config(BaseModel):
    groq_latency: float = 0.0  # seconds per lane call
    agent_latency: float = 0.0  # seconds per agent step
    groq_fault: Fault = Fault()
    # Tokens per minute per model, enforced with Groq's headers and 429s. None: no limit.
    groq_tpm: int | None = None
    github_fault: Fault = Fault()
    github_rate_limit: int = 5000  # requests per window per installation, like GitHub's
    github_rate_window: float = 3600  # seconds; shorter makes a rate-limit test quicker
    review_fail_status: int | None = None  # store the review, then answer with this status
    review_hold_seconds: float = 0.0  # store the review, then wait this long before answering


class PullIn(BaseModel):
    """A PR opened or pushed to: the fakes serve this head from now on."""

    installation_id: int
    repo_id: int
    repo: str
    number: int
    sha: str
    draft: bool = False


@dataclass
class Pull:
    installation_id: int
    repo_id: int
    repo: str
    number: int
    head: str
    draft: bool
    updated_at: str
    state: str = "open"

    def to_json(self) -> dict:
        return {
            "number": self.number,
            "title": f"Load test PR {self.number}",
            "state": self.state,
            "draft": self.draft,
            "head": {"sha": self.head},
            "user": {"login": "loadtest-user"},
            "updated_at": self.updated_at,
        }

    def repo_json(self) -> dict:
        owner = self.repo.split("/")[0]
        return {"id": self.repo_id, "full_name": self.repo, "owner": {"login": owner}}


class State:
    def __init__(self):
        self.config = Config()
        self.pulls: dict[tuple[str, int], Pull] = {}
        self.commits: dict[str, dict[str, str]] = {}  # head SHA -> head files
        self.base: dict[str, dict[str, str]] = {}  # head SHA -> base files
        self.reviews: list[dict] = []
        # Head SHA -> (time, worker) of the first file-content request for it: the job started.
        self.first_seen: dict[str, tuple[float, str]] = {}
        self.github_requests: Counter[str] = Counter()  # by installation ("app" for the JWT)
        self.github_errors: Counter[str] = Counter()  # by status
        self.groq_calls: Counter[str] = Counter()  # by "<lane|agent> <status>"
        self.rate_windows: dict[int, tuple[float, int]] = {}  # installation -> (start, count)
        self.tpm_windows: dict[str, tuple[float, int]] = {}  # model -> (start, tokens)


def _installation(request: Request) -> int | None:
    auth = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer inst-(\d+)", auth)
    return int(match.group(1)) if match else None


def _worker(request: Request) -> str:
    return request.headers.get("x-loadtest-worker", "")


def create_app() -> FastAPI:
    app = FastAPI(title="Load test fakes")
    app.state.fakes = State()

    def state() -> State:
        return app.state.fakes

    @app.middleware("http")
    async def github_limits_and_faults(request: Request, call_next):
        """Every GitHub route: count it, apply the per-installation rate limit and any fault."""
        path = request.url.path
        if path.startswith(("/_", "/openai/")):  # admin, codeload, Groq
            return await call_next(request)
        s = state()
        now = time.time()
        installation = _installation(request)
        s.github_requests[str(installation) if installation else "app"] += 1

        headers = {}
        if installation is not None:
            start, count = s.rate_windows.get(installation, (now, 0))
            if now - start >= s.config.github_rate_window:
                start, count = now, 0
            count += 1
            s.rate_windows[installation] = (start, count)
            remaining = s.config.github_rate_limit - count
            reset = start + s.config.github_rate_window
            headers = {
                "x-ratelimit-remaining": str(max(remaining, 0)),
                "x-ratelimit-reset": str(int(reset)),
            }
            if remaining < 0:
                s.github_errors["403"] += 1
                return JSONResponse({"message": "API rate limit exceeded"}, 403, headers=headers)

        if s.config.github_fault.hits(now):
            s.github_errors[str(s.config.github_fault.status)] += 1
            response = s.config.github_fault.response()
        else:
            response = await call_next(request)
        response.headers.update(headers)
        return response

    # --- GitHub: authentication and the App -------------------------------------------------

    @app.post("/app/installations/{installation_id}/access_tokens", status_code=201)
    async def access_token(installation_id: int) -> dict:
        expires = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
        return {"token": f"inst-{installation_id}", "expires_at": expires}

    @app.get("/app")
    async def github_app() -> dict:
        return {"slug": BOT_SLUG}

    @app.get("/app/installations")
    async def installations() -> list[dict]:
        ids = sorted({p.installation_id for p in state().pulls.values()})
        return [{"id": i, "suspended_at": None} for i in ids]

    @app.get("/installation/repositories")
    async def installation_repos(request: Request) -> dict:
        installation = _installation(request)
        repos = {
            p.repo: p.repo_json()
            for p in state().pulls.values()
            if p.installation_id == installation
        }
        return {"total_count": len(repos), "repositories": list(repos.values())}

    # --- GitHub: PRs ------------------------------------------------------------------------

    def find_pull(owner: str, name: str, number: int) -> Pull | None:
        return state().pulls.get((f"{owner}/{name}", number))

    def not_found() -> JSONResponse:
        return JSONResponse({"message": "Not Found"}, 404)

    @app.get("/repos/{owner}/{name}/pulls")
    async def open_pulls(owner: str, name: str) -> list[dict]:
        repo = f"{owner}/{name}"
        pulls = [p for p in state().pulls.values() if p.repo == repo and p.state == "open"]
        pulls.sort(key=lambda p: p.updated_at, reverse=True)
        return [p.to_json() for p in pulls]

    @app.get("/repos/{owner}/{name}/pulls/{number}")
    async def get_pull(owner: str, name: str, number: int):
        pull = find_pull(owner, name, number)
        return pull.to_json() if pull else not_found()

    @app.get("/repos/{owner}/{name}/pulls/{number}/files")
    async def list_files(owner: str, name: str, number: int):
        pull = find_pull(owner, name, number)
        if not pull:
            return not_found()
        return pull_files(state().base[pull.head], state().commits[pull.head])

    @app.get("/repos/{owner}/{name}/contents/{path:path}")
    async def file_content(request: Request, owner: str, name: str, path: str, ref: str):
        files = state().commits.get(ref)
        if files is None or path not in files:
            return not_found()
        state().first_seen.setdefault(ref, (time.time(), _worker(request)))
        return PlainTextResponse(files[path])

    @app.get("/repos/{owner}/{name}/pulls/{number}/reviews")
    async def list_reviews(owner: str, name: str, number: int) -> list[dict]:
        repo = f"{owner}/{name}"
        return [
            {"id": r["id"], "user": {"login": f"{BOT_SLUG}[bot]"}, "body": r["body"]}
            for r in state().reviews
            if r["repo"] == repo and r["number"] == number
        ]

    @app.post("/repos/{owner}/{name}/pulls/{number}/reviews")
    async def create_review(request: Request, owner: str, name: str, number: int):
        s = state()
        pull = find_pull(owner, name, number)
        if not pull:
            return not_found()
        data = await request.json()
        review = {
            "id": len(s.reviews) + 1,
            "t": time.time(),
            "repo": pull.repo,
            "number": number,
            "sha": data["commit_id"],
            "comments": len(data["comments"]),
            "worker": _worker(request),
            # Posted for a head that had already moved on: the job's last check missed it.
            "stale": data["commit_id"] != pull.head,
            "body": data["body"],
        }
        s.reviews.append(review)
        if s.config.review_hold_seconds:
            await asyncio.sleep(s.config.review_hold_seconds)
        if s.config.review_fail_status:
            s.github_errors[str(s.config.review_fail_status)] += 1
            return JSONResponse(
                {"message": "load test fault after storing"}, s.config.review_fail_status
            )
        return {"id": review["id"]}

    @app.get("/repos/{owner}/{name}/tarball/{ref}")
    async def tarball_redirect(owner: str, name: str, ref: str):
        # GitHub answers with a redirect to codeload; the client must follow it.
        return RedirectResponse(f"/_codeload/{owner}/{name}/{ref}", 302)

    @app.get("/_codeload/{owner}/{name}/{ref}")
    async def codeload(owner: str, name: str, ref: str):
        files = state().commits.get(ref)
        if files is None:
            return not_found()
        return Response(
            tarball(files, f"{owner}-{name}-{ref[:7]}"), media_type="application/x-gzip"
        )

    @app.get("/repos/{owner}/{name}/commits/{sha}/check-runs")
    async def check_runs(owner: str, name: str, sha: str) -> dict:
        return {"total_count": 0, "check_runs": []}

    # --- Groq --------------------------------------------------------------------------------

    @app.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        s = state()
        body = await request.json()
        kind = "agent" if "tools" in body else "lane"
        now = time.time()
        if s.config.groq_fault.hits(now):
            s.groq_calls[f"{kind} {s.config.groq_fault.status}"] += 1
            return s.config.groq_fault.response()

        headers = {}
        if s.config.groq_tpm is not None:
            model = body["model"]
            needed = len(json.dumps(body["messages"])) // 4 + TOOL_CALL_OUTPUT_TOKENS
            start, used = s.tpm_windows.get(model, (now, 0))
            if now - start >= TPM_WINDOW_SECONDS:
                start, used = now, 0
            reset = start + TPM_WINDOW_SECONDS - now
            if used + needed > s.config.groq_tpm:
                s.groq_calls[f"{kind} 429"] += 1
                limit_headers = {
                    "retry-after": str(int(reset) + 1),
                    "x-ratelimit-remaining-tokens": str(s.config.groq_tpm - used),
                    "x-ratelimit-reset-tokens": f"{reset:.2f}s",
                }
                return JSONResponse(
                    {"error": {"message": "rate limit"}}, 429, headers=limit_headers
                )
            used += needed
            s.tpm_windows[model] = (start, used)
            headers = {
                "x-ratelimit-remaining-tokens": str(s.config.groq_tpm - used),
                "x-ratelimit-reset-tokens": f"{reset:.2f}s",
            }

        await asyncio.sleep(s.config.agent_latency if kind == "agent" else s.config.groq_latency)
        message = agent_reply(body) if kind == "agent" else lane_reply(body)
        s.groq_calls[f"{kind} 200"] += 1
        # Usage is tiny on purpose: the daily token budgets would otherwise stop the run.
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        return JSONResponse({"choices": [{"message": message}], "usage": usage}, headers=headers)

    # --- Admin -------------------------------------------------------------------------------

    @app.post("/_admin/reset")
    async def reset() -> dict:
        app.state.fakes = State()
        return {"status": "reset"}

    @app.get("/_admin/config")
    async def get_config() -> Config:
        return state().config

    @app.put("/_admin/config")
    async def set_config(config: Config) -> Config:
        state().config = config
        return config

    @app.post("/_admin/pulls")
    async def push(pull: PullIn) -> dict:
        """A PR is opened, or a new commit is pushed to it."""
        s = state()
        base, head = pr_files(pull.number, pull.sha)
        s.base[pull.sha], s.commits[pull.sha] = base, head
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        s.pulls[(pull.repo, pull.number)] = Pull(
            pull.installation_id,
            pull.repo_id,
            pull.repo,
            pull.number,
            pull.sha,
            pull.draft,
            updated_at,
        )
        return {"status": "ok"}

    @app.get("/_admin/clock")
    async def clock() -> dict:
        return {"now": time.time()}

    @app.get("/_admin/progress")
    async def progress() -> dict:
        s = state()
        reviewed = {r["sha"] for r in s.reviews}
        heads = [p.head for p in s.pulls.values() if p.state == "open" and not p.draft]
        return {"heads": len(heads), "reviewed": sum(h in reviewed for h in heads)}

    @app.get("/_admin/records")
    async def records() -> dict:
        s = state()
        return {
            "pulls": [
                {
                    "repo": p.repo,
                    "number": p.number,
                    "head": p.head,
                    "state": p.state,
                    "draft": p.draft,
                }
                for p in s.pulls.values()
            ],
            "reviews": [{k: v for k, v in r.items() if k != "body"} for r in s.reviews],
            "first_seen": {sha: {"t": t, "worker": w} for sha, (t, w) in s.first_seen.items()},
            "github_requests": dict(s.github_requests),
            "github_errors": dict(s.github_errors),
            "groq_calls": dict(s.groq_calls),
        }

    return app


def lane_reply(body: dict) -> dict:
    """The correctness lane reports one bug on the chunk's first added line; the other lanes
    find nothing. Chunk lines look like "   12 + code"."""
    findings = []
    if body["response_format"]["json_schema"]["name"] == "correctness_findings":
        match = re.search(r"^\s*(\d+) \+ ", body["messages"][-1]["content"], re.MULTILINE)
        if match:
            line = int(match.group(1))
            findings.append(
                {
                    "line_start": line,
                    "line_end": line,
                    "category": "bug",
                    "severity": "high",
                    "title": "Division by zero on an empty list",
                    "explanation": "len(items) can be 0.",
                    "suggested_fix": None,
                }
            )
    return {"role": "assistant", "content": json.dumps({"findings": findings})}


def agent_reply(body: dict) -> dict:
    """Confirm on the first step, citing the finding's own line: it's in the excerpt the
    agent was shown, so the evidence check accepts it."""
    task = body["messages"][1]["content"]
    path, line = re.search(r"^Finding: \S+ \S+ in (\S+), lines? (\d+)", task, re.MULTILINE).groups()
    arguments = {
        "verdict": "confirmed",
        "reason": "the list can be empty",
        "evidence": [{"path": path, "line": int(line), "why": "the division"}],
    }
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "submit_verdict", "arguments": json.dumps(arguments)},
    }
    return {"role": "assistant", "content": None, "tool_calls": [call]}


app = create_app()
