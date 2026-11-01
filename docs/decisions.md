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

**Cross-lane duplicates aren't merged yet.** *(Fixed in Phase 3a.)*
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

**Known cost: retries re-run the lanes.** *(Fixed in Phase 3a by the cache.)*
If posting fails with a temporary error after the lanes ran, the retry starts over and spends the
tokens again. Phase 3's cache fixes this.

### Known issues from the real-GitHub test (PR #4, run 5), to fix in Phase 3 with prompts v2

**Cross-lane duplicates get posted.** *(Fixed in Phase 3a; see below.)*
Correctness flagged an IndexError on lines 9–11 (`critical`), and maintainability flagged lines
8–12 as a low `idiom` finding about the same loop. Both were posted. The fix: when line ranges
overlap across lanes, keep only the most severe. That's a change to `aggregate.py`, not only to
the prompts. The trade-off: a different, genuinely separate issue on overlapping lines (e.g. a
low security note next to a high bug) gets dropped too. Phase 5 measures how often.

**Line ranges are too wide.** *(Fixed in Phase 3a by prompts v2; see below.)*
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

## Phase 3a — Cache, budgets, cost, prompts v2

The plan is in `docs/plans/phase-3.md`.

**The cache key.**
sha256 of the repo ID, lane, the lane's full prompt text, model, file path, and the chunk's lines
*without line numbers*. The spec's key is (path + content + prompt version); the additions are
deliberate:
- **No line numbers:** a hunk that only moved down the file still hits.
- **Prompt text, not the version label:** editing a prompt can never silently reuse old answers.
- **Model:** a different model never reuses another's answers.
- **Repo:** findings can quote code, so they're never shared across repos.

Cached findings store line numbers relative to the chunk's first line, and are shifted to its
current position on a hit.

**The cache lives in Postgres, and is written after every call.**
Render Key Value loses data on restart, so Redis would forget everything a restart ago. Writing
after each successful call (not at the end of the run) means a retry, a stale-run takeover, or a
run cut off by the deadline reuses everything already paid for. Tested: a failed post followed by
a retry makes zero new LLM calls. Failed calls aren't cached; empty answers are. Size is about
1 KB per lane call, with no cleanup job yet.

**Usage is added to the run after every call, not written at the end.**
Found while testing retries: writing usage at the end lost the first attempt's spend whenever it
failed, so a retried run reported $0. Each call now adds to the run's columns with
`col = col + value`, so `tokens_in`, `tokens_out`, `llm_calls`, `cached_calls`, `tokens_saved`
and `cost_usd` are totals over all of a run's attempts.

**`tokens_saved` only counts other runs' answers.**
Each cache entry records the run that paid for it. A hit on another run's entry (a re-push) adds
that entry's original tokens to `tokens_saved`; a retry reusing its own run's calls adds nothing.
So "the cache saved X% of tokens on re-pushes" is
`tokens_saved / (tokens_saved + tokens_in + tokens_out)` without retries inflating it. Tested.

**Per-PR budget: 50K tokens per attempt.**
Before each LLM call (cache hits are free), the call's estimate (prompt characters ÷ 4, plus the
expected output) is checked against what this attempt has spent. If it won't fit, the call is
skipped as "PR token budget". It's per attempt rather than per run because a retry mostly hits
the cache anyway.

**Daily budget: 180K tokens per model per UTC day, counted in Postgres.**
`daily_token_usage(day, model, tokens)` is updated with an atomic upsert after every call, and
checked before every call. It lives in Postgres because it must survive restarts. Concurrent calls
can overshoot by at most the calls in flight (3 × ~3K tokens), and Groq's own daily limit stays
the hard backstop. Assumption: Groq's daily window resets at midnight UTC. Their docs don't say;
if it's rolling, a deferred job may hit a 429 and wait longer.

**The 20K daily headroom won't cover the Phase 4 agent.**
180K of each model's 200K is the pipeline's. The remaining 20K is for smoke tests and slack, not
for verification agent runs, which make several tool-calling steps per finding. Phase 4 must
decide how each model's daily budget is split between the pipeline and the agent (for example, a
lower `DAILY_TOKEN_BUDGET` for the lanes plus a separate agent budget).

**When budgets run short, the least important work is left out.**
Calls are ordered source, then tests, then docs, and within each group the chunks with the most
added lines go first.

**Nothing reviewable under the daily budget: defer to just after midnight UTC.**
If the daily budget stopped every call and nothing came from the cache, there's nothing worth
posting. The job raises `RetryableError` with `retry_after` = seconds to midnight + 5 minutes, so
the usual retry path releases the run and defers the job. That uses one of the job's 5 tries. If
the deferred job is lost (Redis restart), the run is `queued`, and the reconciler re-enqueues it
while the PR is inside its 24-hour lookback. If some calls ran, the review is posted, listing
"not run (daily token budget)".

**Cost: list prices in code, summed per call, stored with 9 decimal places.**
`app/llm/pricing.py` holds Groq's on-demand prices with the source and date: gpt-oss-120b $0.15 /
$0.60, gpt-oss-20b $0.075 / $0.30 per million tokens in/out. Sources disagreed, and Groq's console
is authoritative. `cost_usd` was widened from `Numeric(10, 6)` to `Numeric(12, 9)`: one call costs
about $0.00003, and with per-call additions, 6 decimal places rounded every addition by up to a
few percent (a test caught it). A model with no known price counts as $0, with a warning, rather
than failing a review over a reporting number.

**Prompts v2: point at the exact line.**
`prompts/v2/common.md` replaces "use the narrowest range" (which the model ignored) with an
explicit section: one line is the normal case, a range is at most 5 lines and never a whole
function, plus the `average()` example. v1 stays in `prompts/v1/`, so Phase 5 can compare them.
On PR #4's real code with real Groq, the division by zero went from lines 1–5 to line 5.

**Cross-lane dedupe: overlapping lines keep only the most severe.**
Same file and overlapping lines, whatever the lane or category. On a severity tie, lane order
decides: correctness, then security, then maintainability. On PR #4's real code, maintainability's
note about the `get_user` loop (line 9) is now dropped next to correctness's critical IndexError
(lines 9–11).

**Maintainability runs on gpt-oss-20b (`GROQ_SMALL_MODEL`).**
Each model has its own Groq limits, so moving the lowest-stakes lane takes about a third of the
calls off gpt-oss-120b, the bottleneck: roughly 1.5× throughput, while correctness and security
keep the stronger model. The daily budget and the cost are per model. The 20B model handled the
strict schema without problems in the real-Groq check.

**Hand-written migration parts.**
Autogenerate created the two tables and the counters. The `cost_usd` precision change is written
by hand, because autogenerate doesn't compare numeric precision. `alembic check` passes.

## Phase 3b — Static tools

**Ruff runs on every reviewed Python file, with our rules only.**
`ruff check --isolated --select F,B,UP,SIM`, with the file's text on stdin and `--stdin-filename`,
so no checkout is needed. `--isolated` means the repo's own Ruff config is never read, so a PR
can't silence our rules. A test checks that a repo `per-file-ignores` entry is ignored (a plain
`ignore` wouldn't prove it, since the CLI's `--select` already overrides that). The binary comes
from `ruff.find_ruff_bin()`, not PATH. Ruff moved from the dev dependencies to the runtime
dependencies. Peak memory is about 26 MB per file, and at most 4 run at once.

**Tool findings are hints for the lanes, never posted directly.**
Only findings on lines the PR added are kept: problems that were already there aren't the PR's
doing. They are routed by rule:
- **correctness:** Ruff `F` (pyflakes), Ruff `B` (bugbear), and syntax errors (`invalid-syntax`)
- **maintainability:** Ruff `UP` and `SIM`
- **security:** Semgrep

Each lane sees only its own hints, listed after the code *inside* the untrusted block, because
messages quote identifiers from the code.

**Prompts v3 say how to treat hints.**
Treat them as hints, check each one against the code, report only real problems in your own
words, and keep reviewing what the tools missed. v1 and v2 stay. On a planted file with real Groq,
Ruff's F821 and Semgrep's `shell=True` hint each led to a single-line finding in the model's own
words.

**Hints are part of the cache key.**
The same code with different tool output can get a different answer, so the hints text for that
lane joins the key. Lanes that see no hints keep the same key.

**A tool problem never fails a review.**
All tools run through one helper (`app/static/process.py`): if a tool can't start, times out
(Ruff 10 s, Semgrep 120 s), exits with an error, or prints something we can't parse, it's logged
and the review continues without it.

**Semgrep is behind `SEMGREP_ENABLED`, off by default.**
In the container, one scan of a small file peaked at 143 MB, on top of the app itself, and real
scans use more. That doesn't fit next to the app on a 512 MB instance. Semgrep is for evals and
bigger hosts.

**Semgrep is an isolated tool, not a dependency group (a change from the plan).**
The plan said an optional dependency group. But current Semgrep pins
`opentelemetry-api < 1.38`, while our FastAPI needs `>= 1.44`. A group could only resolve to the
mid-2024 release (1.79), which predates native Windows support. Since we only run Semgrep's CLI and
never import it, it gets its own environment:
- **Docker:** `--build-arg WITH_SEMGREP=true` runs `uv tool install` into `/opt/uv-tools`.
- **CI:** `uv tool install` before the tests, so the real-Semgrep tests run.
- **Local:** any separate install (e.g. `uv tool install`) on PATH.

It's pinned to 1.180.0 (released 2026-10-07). 1.183.0 was only 3 days old. The Semgrep image is
1.39 GB against 523 MB for the default one.

**Our Semgrep rules.**
`app/static/semgrep_rules.yml` has 7 Python rules: `subprocess` with `shell=True`, `eval`/`exec`
of non-literals, `yaml.load` without a safe loader, `pickle.load(s)`, `verify=False`, SQL built
with `%`, `.format`, f-strings or `+`, and hard-coded secrets (a name like
password/secret/api_key/token assigned a string of 8+ characters). They were validated with
`semgrep --validate` and against a sample with one hit per rule plus safe lookalikes that must
not match. That's the "Semgrep secrets rule" that stands in for gitleaks.

**How Semgrep is run.**
The reviewed `.py` files are written to a temp folder (with a guard against paths that would leave
it), and `semgrep scan` runs there. Since only the PR's Python files are in that folder, there's
no `.semgrepignore` or other config of the PR's for Semgrep to pick up. Semgrep's default ignore
list skips `tests/` folders (checked). That's accepted: test files rank lowest, and the security
lane still reviews them.

## Phase 4a — Verification agent (on its own)

The plan is in `docs/plans/phase-4.md`. 4a builds and tests the agent; 4b wires it into the worker.

**The verdict is a tool, because Groq can't combine strict structured outputs with tool calling.**
`submit_verdict(verdict, reason, evidence)` sits next to the four read tools. Every step sends
`tool_choice: "required"`, so the model never answers in prose and no verdict is guessed from free
text. The last allowed step names `submit_verdict` in `tool_choice`, forcing it. Its arguments are
checked with Pydantic; invalid ones go back to the model as the tool result. Rejected: a final
strict-JSON call without tools, which would resend the whole conversation once more.

**Groq checks tool arguments against the schema.**
In the real run, one `read_file` call failed with a 400 `tool_use_failed` ("Tool call validation
failed"). That raises `ToolCallFailed`, which costs the step, and the loop asks again; the next try
worked. Any other Groq error ends the finding as `uncertain`, and a 401/403 still fails the run.

**Stopping rules: a `for` loop over 6 steps, a 12K token cap and 120 s per finding.** *(Phase 4b: the 120 s is working time, rate-limit waits left out; see below.)*
Before each call the request is estimated (characters ÷ 4, tool schemas included). If it doesn't
fit what's left, the finding ends `uncertain` ("token limit"). If the call after it wouldn't fit,
this call is forced to be the verdict, while there's still room for one. A repeated identical
tool call isn't run again, and extra tool calls in one reply are answered "not run": gpt-oss on
Groq doesn't do parallel tool calls anyway, and we send `parallel_tool_calls: false`.

**Evidence must point at lines the agent was shown.**
That's the excerpt, `read_file` results, and search or caller matches. A `confirmed` or `dropped`
verdict with no such evidence becomes `uncertain`. It catches made-up evidence, and it means an
injection that wants a real finding dropped has to cite real code.

**The agent sees a window of the chunk, not all of it.**
The finding's lines plus 20 lines either side. A chunk can be ~2000 tokens, and every step resends
it; the tools can read more when needed. In the real run, the first request was ~1250 tokens.

**Tool results and the candidate are untrusted.**
Each tool result is wrapped in `BEGIN/END UNTRUSTED TOOL RESULT <id>` with a new random ID, so
file content can't close the block. The candidate's title and explanation come from a lane that
read the PR, so they sit in the untrusted PR block with the excerpt. `prompts/agent/v1.md` says
both are data. The tools are read-only and the verdict only filters, so the worst an injection
can do is hide or keep a finding, not post new text.

**The repo snapshot: a tarball streamed to disk, extracted member by member.**
`GET /repos/{repo}/tarball/{sha}` redirects to codeload; httpx drops our token on that cross-host
redirect (tested). The download stops past 50 MB. Extraction reads the archive as a stream and
never calls `extractall`: only regular files, top folder stripped, a path guard, at most 512 KB per
file, no binaries (a NUL byte in the first 8 KB), at most 100 MB or 20K files. If anything fails,
the repo tools say "repository files are unavailable" and the review goes on. Measured in the
container on FastAPI 0.115.0 (12.5 MB tarball, 2,256 files kept): extraction 1.3 s, a search 0.3–0.4 s,
about 7 MB of extra peak memory.

**`search_repo` is plain text, never a regex.**
A pattern chosen by the model (or by an injection) could take forever to match. `find_callers`
builds its own escaped whole-word regex, and leaves out the symbol's `def`/`class` lines. It's
grep-based, with the limits noted before Phase 0.

**CI results are fetched once per attempt; errors come back as text.**
`get_ci_results` lists the commit's check runs (name, status, conclusion, title cut short). A
GitHub error, e.g. a missing Checks permission, never fails the review. CI is often still running
when the agent looks.

**The model's reasoning isn't sent back.**
Only the assistant message's content and tool calls go into the history, which saves tokens. Groq
accepted it in the real run.

**`agent_steps`: one row per LLM call.**
`tool` is NULL when the call failed or called no tool. Arguments are stored even when they don't
parse, for debugging. Tokens are split into in and out for cost. Rows are deleted with their
finding (ON DELETE CASCADE), so a retry that replaces a run's findings leaves no stray steps.
`findings` gains `verdict_reason` and `evidence`.

**The real-Groq check (no posting).**
A file with an f-string SQL query whose column comes from an allow-list in another file, and an
average that divides by `len()` of a list that can be empty. Both lanes flagged the SQL; the agent
dropped both, citing the allow-list lines, and confirmed the division by zero, citing the caller.
3 findings, 3–4 steps each, about 4.5K tokens per finding ($0.0024 in total). That's well under
the plan's 10–12K estimate. The long step latencies (up to 54 s) were the per-minute token limit
waiting after the lane calls.

### Notes for Phase 5

**With about 5 agent PRs a day, the eval set may need to shrink to about 20 PRs.**
The agent's 70K daily budget limits how many PRs a day can run in "pipeline + agent" mode.

**A possible quota saver to measure: skip the agent for findings Ruff or Semgrep back up.**
A lane finding on the same line as a static-tool finding is already corroborated. Phase 5 should
measure how often the agent would have dropped those before skipping it for them.

## Phase 4b — The agent in the review job

**Rate-limit waits don't count against a finding's time.**
In 4a, steps waited up to 54 s for Groq's per-minute reset, and with 120 s per finding passed to
Groq as a deadline, good checks could end "uncertain (time limit)" because of the shared quota,
not the agent. Now the client reports how long each call waited (`waited_seconds`: rate-limit
waits, 429s and 5xx backoff), and the 120 s counts only working time: calls and tools. Once it's
used up, the next step is the forced verdict rather than an `uncertain`. The agent's 720 s
deadline counts waits and stays the hard limit. Chosen over simply raising the per-finding limit,
which would let one slow finding use the whole window, and would still mix quota with agent
time. In the real end-to-end run, a step that waited 51 s for the rate limit didn't count.

**The flow: findings are saved before the agent runs.**
Aggregate, then save every candidate (replacing the attempt's findings and their steps), then the
agent, then the moved-on check, then post, then mark the posted ones. Each agent step and each
verdict is written as it happens, so a crash keeps what was done. A new commit that arrives while
the agent runs still skips posting, and the verdicts stay in the DB.

**Only confirmed findings are posted.**
- **Confirmed:** an inline comment, with "Checked against `path:line`" for up to 3 evidence
  locations (locations only, through `safe()`).
- **Uncertain:** a collapsed "Uncertain (not posted)" section with the agent's one-line reason.
- **Not verified** (time or budget ran out before it started): a collapsed "Not verified (not
  posted)" section with the reason. `verdict` stays NULL.
- **Dropped:** only counted in the summary. Evidence and steps are in the DB.

The summary says which model checked the findings, the verdict counts, and the agent's share of
the cost.

**The daily budget is split by purpose.**
`daily_token_usage` gained a `purpose` column (`pipeline` or `agent`) in its primary key, so the
lanes can't use the agent's share or the other way round. Per model: 110K for the lanes
(`PIPELINE_DAILY_TOKEN_BUDGET`), 70K for the agent (`AGENT_DAILY_TOKEN_BUDGET`, used on
`GROQ_MODEL` only), 20K left over. The migration is hand-written (autogenerate doesn't see
primary key changes): existing rows become `pipeline`, and the downgrade folds agent tokens into
the pipeline row for the same day and model, so no spend is forgotten (checked on the dev DB).

**Agent budgets are checked before each finding starts, with an estimate.**
A finding starts only if 5K tokens (about what one cost in the real runs) are left in both the
attempt's 30K (`AGENT_PR_TOKEN_BUDGET`) and the day's 70K. In flight, a finding can spend up to
its 12K cap, so the budgets can overshoot by at most the findings in flight (2 × 12K). If no
finding could start, the repo isn't even downloaded. A finding isn't started with less than 30 s
left before the agent's deadline.

**Two findings at a time, most severe first, one shared snapshot.**
Aggregate's ranking decides the order. The repo is downloaded once per attempt, after
aggregation, and only if there's something to check; its temp folder is deleted afterwards.
Two at a time keeps one finding's tool time from idling the quota; more wouldn't help, since
Groq's tokens per minute are the bottleneck.

**Deadlines: lanes by 300 s, agent by 720 s, job timeout 900 s.**
Both counted from the job's start. The lanes keep the same 300 s with the agent off, so "pipeline
only" and "pipeline + agent" review the same chunks.

**`AGENT_ENABLED` (default true) and `review_runs.agent_enabled`.**
Off means exactly Phase 3: every finding that passes aggregation is posted, `verdict` stays NULL,
and nothing is downloaded. The run records the mode, so Phase 5 can tell the two kinds of run
apart. The lane cache doesn't depend on the flag.

**Run totals include the agent.**
Agent steps add to the run's `tokens_in`, `tokens_out`, `llm_calls` and `cost_usd` after every
step (the Phase 3a rule), and to the agent's daily count. The agent's share of a run is the sum of
its `agent_steps`.

**Known cost: a retry after the agent re-runs the agent.**
There's no verdict cache: if posting fails and the job retries, the lanes come from the cache but
the agent checks every finding again. Retries after a failed post are rare; a verdict cache keyed
by (run, finding) can come later if Phase 6 shows otherwise.

**The real end-to-end run (real Groq, mocked GitHub, nothing posted).**
The whole job on the planted pair from 4a: the security lane's SQL injection finding was dropped,
citing the allow-list, and the division by zero was confirmed and posted with "Checked against
`app/orders.py:7`, `app/api.py:10`". 9 LLM calls in total, $0.0023, of which the agent $0.0015.
