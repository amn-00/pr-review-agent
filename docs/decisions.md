# Design decisions

Short notes on what we decided and why. Newest phase at the bottom.

## Decided during spec review (before Phase 0)

**Repo search uses a tarball at the head SHA, not GitHub code search.**
GitHub code search only indexes the default branch, so it can't see the PR's code. The agent's
`search_repo` / `read_file` work on a tarball of the head SHA, extracted by streaming with a size
cap so a huge repo can't exhaust 512 MB.

**`find_callers` is grep-based.**
It searches for the symbol name as text. It will miss dynamic calls and can match unrelated
symbols with the same name. Real call-graph analysis per language is out of scope.

**No ESLint. Ruff and Semgrep run with our own rules only.**
ESLint configs are JavaScript, so running a PR's config means running untrusted code. Ruff config
is static TOML and Semgrep uses our rule files. Semgrep is memory-heavy, so it sits behind a flag:
off in prod if memory is tight, on for evals.

**Dedupe happens at two levels.**
The `X-GitHub-Delivery` ID in Redis filters exact replays cheaply. The real guarantee is the arq
job ID `{repo}:{pr}:{sha}`: arq won't enqueue a second job with the same ID, which also covers
separate deliveries for the same commit.

**A hidden `<!-- run:{id} -->` marker in each posted review.**
If a worker crashes after GitHub accepts the review but before we record it in Postgres, the
retry would post twice. Before posting, the worker lists the PR's reviews and looks for its marker.

**The cache stores findings with chunk-relative line numbers.**
An unchanged hunk can move when lines are added above it. Storing `line - chunk_start` lets us
reuse cached findings at the hunk's new position.

**The hallucination guard checks commentable lines only.**
GitHub only accepts inline comments on lines inside a diff hunk (new side, including context
lines), not on any line of the file. We build that exact set from the diff and check every finding
against it.

**In prod the arq worker runs inside the API process.**
Render's free tier has no background workers. Running the worker in the API process keeps one
service within 512 MB. Locally, docker-compose still runs them as separate containers.

**A reconciler is the safety net for missed webhooks (planned for Phase 2).**
Render free services sleep, and GitHub does not retry failed deliveries, so webhooks can be lost.
On startup and every few minutes, a job lists open PRs per installation and enqueues any head SHA
without a completed run. Webhooks are the fast path. It lands in Phase 2 because "completed run"
only means something once real reviews exist.

**Redis is Render Key Value, with no persistence.**
Upstash's free tier caps commands, and arq polls Redis constantly. Render Key Value has no command
cap, but it loses data on restart: queued jobs and dedupe keys disappear. The reconciler re-enqueues
lost work, and the run marker plus the DB prevent duplicate reviews.

**A global Redis-backed rate limiter for Groq.**
Groq free-tier limits are per account, not per PR, so every worker shares one limiter. The eval
set is sized to fit the daily free quota.

**`cost_usd` is hypothetical.**
Groq's free tier costs $0, so we compute cost from list prices in config. That keeps per-run cost
meaningful for comparing pipeline-only vs pipeline + agent.

**The per-installation concurrency limit is a Redis semaphore.**
arq has no per-key concurrency control, so a small Redis counter with a TTL limits jobs per
installation.

## Phase 0 — Scaffold

**uv for dependency management.**
`uv.lock` pins every transitive version, so CI, Docker and local dev install the same packages.
`pyproject.toml` holds only lower bounds.

**One Docker image, two commands.**
The API and the worker share code and dependencies, so they share an image. Compose overrides the
command for the worker (`arq app.worker.WorkerSettings`). That also matches the prod plan of one
process.

**`create_app()` factory.**
Tests build a fresh app instead of importing a global one, so later phases can swap in test
settings or fake clients per test.

**Settings validate URL types.**
`REDIS_URL` and `DATABASE_URL` are typed as `RedisDsn` / `PostgresDsn`, so a typo in an env var
fails at startup instead of at the first connection.

**`/health` is liveness only for now.**
It checks nothing but the process itself. A readiness check covering Postgres and Redis comes in
Phase 1, once the app actually uses them.

**Postgres 16 locally.**
Matches a version Neon supports, so migrations behave the same locally and in prod.
