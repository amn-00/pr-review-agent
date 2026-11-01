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

**A reconciler is the safety net for missed webhooks (planned for Phase 2).** *(Built in Phase 2b, with a 24-hour lookback; see below.)*
Render free services sleep, and GitHub does not retry failed deliveries, so webhooks can be lost.
On startup and every few minutes, a job lists open PRs per installation and enqueues any head SHA
without a completed run. Webhooks are the fast path. It lands in Phase 2 because "completed run"
only means something once real reviews exist.

**Redis is Render Key Value, with no persistence.**
Upstash's free tier caps commands, and arq polls Redis constantly. Render Key Value has no command
cap, but it loses data on restart: queued jobs and dedupe keys disappear. The reconciler re-enqueues
lost work, and the run marker plus the DB prevent duplicate reviews.

**A global Redis-backed rate limiter for Groq.** *(Superseded in Phase 2a: per-process limiter, see below.)*
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

**Worker crashes: arq re-runs the job, and the run marker prevents a second post.** *(Since Phase 2a the marker is in the review body and is searched in our bot's reviews.)*
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

`repos.installation_id` has no index yet. *(Still unread after Phase 2b: the reconciler lists repos from GitHub, not from our tables.)*

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

**Claiming a run: one atomic `UPDATE`, with stale takeover.** *(Phase 2a raised the job timeout, and so the stale threshold, to 900 s.)*
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

**The marker search reads every page of comments.** *(The fake-marker gap is closed in Phase 2a: see "Trusting the marker".)*
`list_issue_comments` asks for 100 per page and follows the `Link: rel="next"` header until the
end, so the marker is found on busy PRs. Tested with the marker on page 2. We don't filter to our
App's own comments, so a PR author could post a fake marker and suppress the placeholder comment
for their PR. That's harmless now. It's worth closing in Phase 2, before real reviews depend on the
marker, by checking `performed_via_github_app.id`.

**The installation token cache is keyed by installation ID.**
`GitHubAppAuth._tokens` maps installation ID to (token, expiry), so one installation's token is
never used for another installation's repos. Tested in `test_tokens_are_cached_per_installation`.

## Phase 2a — Pipeline and real reviews

**One review per run, no placeholder comment.**
The spec asks for one review per run with a summary, so the "review started" comment is gone and
the run marker lives in the review body. A clean PR still gets a review ("No issues found"), so
every run's outcome is visible on the PR.

**Trusting the marker: only reviews authored by our bot account.**
Reviews have no `performed_via_github_app` field (issue comments do), so we compare the review's
author with `<app-slug>[bot]`, looked up once via `GET /app`. GitHub usernames can't contain `[`
or `]`, so no human can author as that account. A pasted marker in a human's review is ignored
(tested).

**Job duration: a 900 s timeout plus a 600 s deadline, no heartbeat.**
Reviews wait on Groq's 8K tokens/minute, so 300 s was too short. The LLM work must end 600 s after
the job starts; chunks not reached are listed as skipped, and the rest of the 900 s covers fetching,
posting and GitHub retries. `claim_run` uses the same `JOB_TIMEOUT_SECONDS`, so a `running` run is
stale after 900 s (tested at 600 s: not taken over).

**Why not a heartbeat.**
arq re-runs a crashed job only once its in-progress key expires (`job_timeout + 10 s`), and until
then the crashed job's ID blocks a replacement from the webhook or reconciler. A heartbeat could
notice a dead worker sooner, but nothing could act on it without giving up arq's job-ID dedupe.
It would also add a background writer whose failure (a blocked event loop) makes a live job look
dead. The cost of this choice: a crashed review restarts after 15 minutes, not 5.

**Under the deadline, the least important chunks are skipped first.**
Chunks are ordered source code, then tests, then docs (`*.md`, `*.rst`, `*.txt`, `docs/`), keeping
diff order within each group. All lanes of a chunk are queued together, and workers take calls in
that order, so the time limit cuts off the tail.

**Prompt injection: PR content is data, in several layers.**
- **Separation:** PR content, file names included, goes only in the user message, between
  `BEGIN/END UNTRUSTED PR CONTENT <id>` lines, with a random ID per call so the PR can't forge
  the end marker. The system prompt is a fixed file that says to treat that block as code and
  never follow instructions in it.
- **No power:** lanes have no tools, and strict structured output means the model can only return
  findings. The worst an injection can do is suppress or invent findings. Invented lines are
  dropped by the hallucination guard, and Phase 4's agent verifies the rest.
- **Output sanitizing:** before our bot posts model text or file names, `<!--` is escaped (a
  planted fake marker could stop a later run from posting) and `@mentions` are defused.
- **Limit:** no prompt can guarantee the model is never swayed into staying quiet. Phase 5's eval
  set should include an injection PR to measure it. Tests cover the separation and the sanitizing.

**Cross-lane duplicates aren't merged yet.**
Dedupe merges candidates with the same file, category and overlapping lines, keeping the most
severe. Each lane has its own categories, so the same lines flagged by two lanes (e.g. a bug and a
readability note) both get posted. Phase 5 will measure how often that happens before we add a rule.

**Groq rate limiting is per process, driven by Groq's headers.**
This supersedes the spec-review plan for a Redis-backed limiter. Every response reports the tokens
left this minute (`x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens`), per model, and the
client waits for the reset before sending a call that won't fit. In prod the worker runs inside the
single API process, so this is effectively global. Phase 6 load tests will show whether Redis is
needed.

**Groq errors.**
- **429:** wait `retry-after`, then retry.
- **5xx and timeouts:** back off 2 s, then 4 s, for up to 3 attempts.
- **400** (e.g. output that fails the schema): that one call fails and the review goes on.
- **401/403:** `NonRetryableError`, so the run fails. Otherwise a bad key would post a misleading
  "No issues found".

No wait may pass the deadline: if it would, the call counts as skipped instead.

**Structured output: strict JSON schema, written by hand, validated again with Pydantic.**
Strict mode guarantees the shape, but needs every field `required` and
`additionalProperties: false`, so the schema is written out rather than generated (a test keeps it
in sync with the model). Each lane's schema only allows that lane's categories. `reasoning_effort`
is `low` and output is capped at 2048 tokens, to save the token budget.

**The model doesn't return the file name.**
The spec's lane output includes `file`, but a chunk is always one file, so we set it from the chunk.
That removes one thing the model could get wrong.

**Chunking: one hunk plus 20 lines of context, merged when windows overlap, split at about 2000 tokens.**
Context comes from the full file at the head SHA (Contents API, raw media type). Tokens are
estimated as characters ÷ 4, which is enough for sizing and needs no tokenizer dependency.

**Files not reviewed.**
Deleted files, files without a patch (binary or too large for GitHub to diff), and lockfiles are
skipped and listed in the summary. Lockfiles would burn the token budget and find nothing.

**Hallucination guard details.**
A finding's first line must be inside a hunk on the new side. A range that leaves that hunk shrinks
to its first line, because GitHub rejects multi-line comments that span hunks. Findings about deleted
lines are dropped, since we only comment on the new side.

**The `findings` table keeps every candidate.**
Dropped candidates are stored with `drop_reason` (`not_in_diff` or `duplicate`), so evals can see
how often the guard and dedupe fire. Each attempt replaces the run's findings in one transaction,
so retries never leave duplicates. Indexed on `run_id`, for loading a run's findings.

**`github_comment_id` is renamed to `github_review_id`.**
The migration renames the column by hand, because autogenerate would drop it and add a new one.
`alembic check` confirms the migrations match the models.

**The head SHA is checked again before posting.**
If a new commit arrived while the lanes ran, the run is `skipped` and nothing is posted. The
findings are still saved, so the work isn't lost for evals.

**`max_jobs = 2`.**
Two reviews at once already use up Groq's tokens-per-minute limit, and more would hold memory on
a 512 MB instance while they wait. The per-installation limit waits for Phase 6, where load tests
make it measurable.

**Known cost: retries re-run the lanes.**
If posting fails with a temporary error after the lanes ran, the retry starts over and spends the
tokens again. Phase 3's cache fixes this.

### Known issues from the real-GitHub test (PR #4, run 5), to fix in Phase 3 with prompts v2

**Cross-lane duplicates get posted.**
Correctness flagged an IndexError on lines 9–11 (`critical`), and maintainability flagged lines
8–12 as a low `idiom` finding about the same loop. Both were posted. The fix: when line ranges
overlap across lanes, keep only the most severe. That's a change to `aggregate.py`, not only to
the prompts. The trade-off: a different, genuinely separate issue on overlapping lines (e.g. a
low security note next to a high bug) gets dropped too. Phase 5 measures how often.

**Line ranges are too wide.**
The division by zero in `average()` was posted on lines 1–5 (the whole function) instead of line
5. The v1 prompt already says "Use the narrowest range that shows the problem", so wording alone
isn't enough. v2 should ask for the single line where the problem happens (`line_start ==
line_end`) unless the problem really spans lines, and show an example.

## Phase 2b — Reconciler

**An arq cron job, at worker startup and every 5 minutes.**
arq's cron is `unique` by default, so with several workers only one runs each cycle. It has a
120 s timeout and `max_tries=1`: the next cycle, 5 minutes later, is the retry. The startup run
matters on Render, which wakes the service on an incoming request: the request that woke it may
be the very webhook that got lost. While the service sleeps the cron doesn't fire, so on Render
"within 5 minutes" becomes "when the service next wakes".

**Open PRs come from GitHub, not from our tables.**
Our tables only know repos and PRs that some webhook already told us about. If the first webhook
for a PR is the one that was lost, only GitHub knows the PR exists. One cycle costs, per
installation, one request for its repos plus one request per repo for its open PRs.

**When a head SHA gets a job.**
No run (webhook missed), `queued` (its job may have been lost with Redis), `skipped` (was closed
or a draft, now open at the same head), or `running` for longer than the job timeout (worker
died). Not when `completed`, or when `running` and fresh. Not when `failed` either: only a new
delivery retries a failed run, so a broken PR doesn't burn the token budget every 5 minutes.
Enqueueing uses the same job ID as the webhook, so arq ignores PRs that are already queued or
running.

**Only PRs updated in the last 24 hours.**
A missed webhook always comes from a recent push or open, which bumps the PR's `updated_at`.
Without this, the first cycle after installing the App would review every open PR, months-old
ones included, and could use up a day's Groq budget. PRs are listed newest first, and paging stops
at the first PR older than the window. The cost: a webhook missed and then unnoticed for more than
24 hours is only reviewed on the next push.

**No cap on how many jobs one cycle enqueues.**
The lookback, `max_jobs = 2` and the review deadline already bound the work, and Phase 3's daily
token budget is the real guard. A per-cycle cap would be one more knob for no gain today.

**Errors are contained per installation.**
A GitHub error for one installation is logged and the cycle moves on to the next one. Suspended
installations are skipped, since GitHub refuses them tokens anyway. If listing the installations
itself fails, the cycle fails, arq logs it, and the next cycle tries again.

**The job can't tell where its payload came from.**
The reconciler builds the same trimmed `PullRequestPayload` the webhook enqueues, with
`action="reconcile"`, and passes `delivery_id="reconciler"`, so its jobs are easy to filter in the
logs. An end-to-end test runs reconcile, then the real arq worker, and checks a review is posted.

**`JOB_TIMEOUT_SECONDS` moved to `app/queue.py`.**
The worker uses it as arq's timeout, and the reconciler uses it to spot stale runs. Importing it
from `app.worker` would be circular, since the worker imports the reconciler to register the cron.

**One paging helper for every GitHub listing.**
`_iter_items` follows the `Link` header and yields items, so a caller can stop early (the open-PR
listing does). `installation_id=None` means "authenticate as the App" (JWT, for
`/app/installations`), and `items_key` unwraps endpoints that return `{"repositories": [...]}`
instead of a list.
