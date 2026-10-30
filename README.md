# PR Review Agent

A GitHub App that reviews pull requests: a parallel LLM pipeline for breadth, plus a
tool-using verification agent for precision. See [SPEC.md](SPEC.md) for the full design and
[docs/decisions.md](docs/decisions.md) for the reasoning behind it.

Status: Phase 0 (scaffold).

## Run locally

```sh
cp .env.example .env
docker compose up --build
curl http://localhost:8000/health   # {"status":"ok"}
```

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
