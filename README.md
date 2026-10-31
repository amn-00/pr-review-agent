# PR Review Agent

A GitHub App that reviews pull requests: a parallel LLM pipeline for breadth, plus a
tool-using verification agent for precision. See [SPEC.md](SPEC.md) for the full design and
[docs/decisions.md](docs/decisions.md) for the reasoning behind it.

Status: Phase 1 (webhook → queue → worker posts a "review started" comment).

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
