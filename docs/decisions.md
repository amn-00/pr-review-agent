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

## Phase 1 — Webhook → queue → worker

**Webhook delivery IDs live in Redis only; there is no `webhook_deliveries` table.**
`SET delivery:{id} NX EX 3d` is a cheap first filter for exact replays. The durable guarantee is
the unique `(pr_id, head_sha)` constraint on `review_runs`, which still holds after Redis loses
its data. A table would add a DB write before the 200, against the < 200 ms goal.

**The webhook never touches Postgres; the worker creates the run row.**
That keeps the endpoint fast and keeps it working if Postgres is briefly down. So there is no
`queued` status: a run starts as `running`, then ends as `completed`, `failed` or `skipped`. A CHECK
constraint enforces those four values. *(The status part is superseded: runs now start `queued`;
see "Claiming a run" under Phase 1 review fixes.)*

**Releasing the delivery ID when enqueueing fails.**
If Redis accepts the delivery ID but the enqueue fails, we delete the key and return 503.
GitHub's manual redelivery reuses the same ID, so otherwise it would be wrongly deduped.

**Older commits are skipped by checking GitHub, not by aborting jobs.**
At job start the worker fetches the PR. If it is closed or a draft, or its head SHA has moved on,
the run is `skipped`. GitHub is the source of truth, so this is correct even when jobs run out
of order, and it needs no arq job-abort machinery. Phase 2 repeats the check before posting.

**Errors are classified once, in `app/github/http.py`.**
- Timeouts, connection errors, 5xx and 429 raise `RetryableError`, and so does a 403 that comes
  with `Retry-After` or `X-RateLimit-Remaining: 0`.
- Any other 4xx raises `NonRetryableError`. A 403 without those headers is a permission problem,
  and retrying won't fix it.
- Any other exception is a bug: the run is marked `failed` and arq logs the traceback.

**Retries use arq's `Retry(defer=...)`, with exponential backoff and jitter.**
The base is 5 s, doubling up to a 300 s cap, with a random delay between 50% and 100% of that.
If GitHub sends `Retry-After` or a rate-limit reset time, we wait exactly that long. On the last
try the job marks the run `failed` itself. Otherwise arq would drop the job silently and the run
would be stuck at `running`.

**The GitHub rate limit is respected before hitting it.**
The client remembers `X-RateLimit-Remaining/Reset` per installation from every response. Below
`MIN_RATE_LIMIT_REMAINING` (50, in `app/github/client.py`), it defers the job until the reset instead of using
up the last requests. The numbers are kept in each process's memory, not shared across workers.
That's good enough with one worker; Phase 6 load tests will show whether we need it in Redis.

**Installation tokens are cached in process memory.**
Tokens live 1 hour, and we refresh 5 minutes early. The cost is one token request per process per
hour. Sharing them via Redis would mean storing credentials in Redis for no real benefit.

**The private key is stored as PEM text in one env var, with `\n` escaped.**
That works the same in `.env`, docker-compose and Render's env settings, with no secret files to
mount.

**Worker crashes: arq re-runs the job, and the run marker prevents a second post.**
arq keeps a job in its queue until it finishes. While running, the job holds an in-progress key
that expires after the job timeout, after which another worker picks the job up again. Before
posting, the job looks for `<!-- pr-review-agent run:{id} -->` among the PR's comments. Tested
by simulating "posted, then died" (`test_does_not_post_again_if_marker_already_on_pr`). A real
kill test is in Phase 6.

**Known limitation: arq keeps job results for 1 hour.** *(Fixed: see `keep_result = 0` under
Phase 1 review fixes.)*
arq refuses a job ID whose result key still exists. So a commit whose run failed can't be
re-enqueued for that `(repo, pr, sha)` within the hour, even by a new delivery. Phase 2's
reconciler is the natural place to handle this.

**JSON logs via stdlib `logging` + `contextvars`, with no new dependency.**
The webhook and the job call `set_log_context(delivery_id, repo, pr, sha)`, and the job adds
`run_id`. Every line logged during that request or job carries those fields, including uvicorn's
and httpx's. contextvars are per asyncio task, so concurrent jobs don't mix contexts (tested).
structlog would do the same with more features, but this is about 50 lines.

**Migrations run as a one-shot compose service.**
`migrate` runs `alembic upgrade head`, and `api`/`worker` start only after it succeeds. Two
processes never race to migrate. The prod equivalent is decided in Phase 8.

**Constraint naming convention.**
`MetaData(naming_convention=...)` gives every constraint a predictable name, e.g.
`uq_review_runs_pr_id_head_sha`. Without it, Postgres invents names and later migrations can't
reliably drop or alter constraints.

**Indexes.**
The unique constraints double as the indexes for every Phase 1 lookup:
- `installations(github_installation_id)` and `repos(github_repo_id)` for the upserts
- `pull_requests(repo_id, number)` for finding a PR
- `review_runs(pr_id, head_sha)` for finding a commit's run, and all runs of a PR via the
  leading column

`repos.installation_id` has no index yet; the reconciler (Phase 2) is its first reader.

**Tests use real Postgres and Redis, not SQLite or fakeredis.**
`ON CONFLICT`, CHECK constraints and arq's dedupe are exactly what's being tested, and fakes
behave differently there. Tests use a separate `pr_review_test` database, created and migrated
by the test session, and Redis DB 1. They never touch dev data, and a running dev worker
(Redis DB 0) can't steal test jobs.

**Responses: 202 for queued, 200 for ignored or duplicate.**
GitHub treats every 2xx as delivered. 202 makes "accepted for async processing" visible in
GitHub's Recent Deliveries view.

## Phase 1 review fixes

**Four tuning values are constants, not settings.**
`GITHUB_API_URL`, `MIN_RATE_LIMIT_REMAINING`, `DELIVERY_TTL_SECONDS` and `JOB_MAX_TRIES` sit next to
the code that uses them. Nobody asked for them to be configurable, and each env var is one more
thing to document and get wrong. They can become settings when there's a real need.

**`keep_result = 0`: arq keeps no record of finished jobs.**
With arq's default (1 hour), a finished job's ID can't be enqueued again. That blocked reviewing
a commit after close + reopen, or after a failure, and the reconciler would hit the same block.
Postgres's unique `(pr_id, head_sha)` is the durable dedupe, so arq only needs to dedupe jobs that
are queued or running. Tested end to end with a real arq `Worker`, and the tests fail with 3600.

**The rerun rule: only a `completed` run stops a rerun.**
`failed` and `skipped` runs are retried whenever a new job arrives for that commit, e.g. after
the App's permissions are fixed, or when a PR closed before its job ran is reopened.

**Claiming a run: one atomic `UPDATE`, with stale takeover.**
Runs are created `queued`. A job starts only if a single `UPDATE ... WHERE ... RETURNING` turns the
run into `running`. That succeeds for `queued`, `failed` and `skipped` runs, and for `running`
runs whose `started_at` is older than the job timeout (300 s). The check and the write are one
statement, so two workers can't both claim a run (tested by racing two claims).

**Why the timeout makes a `running` run stale.**
arq cancels a job after `job_timeout`, so no live attempt can be older than that. arq re-runs a
crashed job only after `job_timeout + 10 s`, by which point the run is stale and the re-run takes
it over. A fresh `running` run means another worker is on it, e.g. a duplicate job after Redis
lost its data, so the job leaves it alone and doesn't call GitHub.

**A job releases its run before retrying.**
Before raising `Retry`, the job sets the run back to `queued`. Otherwise its own retry would find
a fresh `running` run and back off as if another worker held it.

**This brings back a `queued` status.**
It means "not held by any worker": just created, or waiting for a retry. A new migration widens the
CHECK constraint. It's written by hand, because autogenerate doesn't detect CHECK changes. The
downgrade turns `queued` runs into `failed`, which keeps them retryable.

**Migrations need only `DATABASE_URL`.**
`Settings` now extends a `DatabaseSettings` base that holds just `database_url`. Alembic's
`env.py` loads only that, so `alembic upgrade` runs without the GitHub secrets. Checked by running
upgrade, downgrade and upgrade with no `.env` and no GitHub env vars.

**The marker search reads every page of comments.**
`list_issue_comments` asks for 100 per page and follows the `Link: rel="next"` header until the
end, so the marker is found on busy PRs. Tested with the marker on page 2. We don't filter to our
App's own comments, so a PR author could post a fake marker and suppress the placeholder comment
for their PR. That's harmless now. It's worth closing in Phase 2, before real reviews depend on the
marker, by checking `performed_via_github_app.id`.

**The installation token cache is keyed by installation ID.**
`GitHubAppAuth._tokens` maps installation ID to (token, expiry), so one installation's token is
never used for another installation's repos. Tested in `test_tokens_are_cached_per_installation`.
