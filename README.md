# PR Review Agent

A GitHub App that reviews pull requests: a parallel LLM pipeline for breadth, plus a
tool-using verification agent for precision. See [SPEC.md](SPEC.md) for the full design and
[docs/decisions.md](docs/decisions.md) for the reasoning behind it.

Status: Phase 4 complete. 3 LLM lanes review the diff, with Ruff (and optionally Semgrep)
findings as hints; a tool-using verification agent then checks each finding against the repo
at the PR's head commit, and only confirmed findings are posted, as one review with inline
comments. Cached by chunk, with per-PR and daily token budgets and cost per run; a reconciler
catches missed webhooks. `AGENT_ENABLED=false` gives pipeline-only reviews.

## Run locally

1. Create a GitHub App and a smee channel: [docs/github-app-setup.md](docs/github-app-setup.md).
2. Start the stack and the webhook forwarder:

```sh
cp .env.example .env        # then fill in the GitHub App values
docker compose up --build   # runs migrations, then api + worker
npx smee-client --url https://smee.io/<your-channel> --target http://localhost:8000/webhook
```

Health checks: `curl localhost:8000/health` (process up) and
`curl localhost:8000/health/ready` (Redis and Postgres reachable).

## Develop

Requires [uv](https://docs.astral.sh/uv/). Tests run against real Postgres and Redis:

```sh
uv sync
docker compose up -d postgres redis
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

New migration after changing `app/db/models.py` (needs only `DATABASE_URL`, and Postgres running):

```sh
uv run alembic revision --autogenerate -m "describe the change"
```
