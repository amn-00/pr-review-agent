# Phase 6c plan: fixes for what the failure tests confirmed

Draft 2026-11-04, awaiting approval. The measured results are in `loadtest/results/report.md`
and in `docs/decisions.md` ("Phase 6 — Measured results"). Fixes 1 and 2 were agreed before the
runs; fix 3 and the concurrency-limit decision are new.

**Done when:**
- K2, G2 (60 s and 180 s), Q1 and Q2 run again and show the gaps closed, with the numbers below.
- N1 has run, and the per-installation limit is decided by the rule in `docs/plans/phase-6.md`.
- `docs/decisions.md` and `docs/engineering-log.md` have the before/after numbers.

## Fix 1: SIGTERM leaves the run `running` (K2) [app/]

**Measured:** 1,035 s from `docker stop` to the 2 in-flight reviews. Arq re-ran both jobs within
2 s, but they found their runs `running` and fresh and exited. The reconciler picked them up only
once they were stale.

**Change (agreed), in `app/worker.py` `review_pr`:** catch `asyncio.CancelledError`, put the run
back to `queued` with the existing `release_run`, then re-raise so arq still re-queues the job.

**Not on a job timeout.** Arq's job timeout also reaches the job as a cancellation. On a timeout
arq doesn't run the job again, so today the run goes stale and the reconciler retries it about
15–20 minutes later. Releasing it would make the reconciler retry it every 5 minutes for 24 hours.
So the run is released only when the job has run for less than `JOB_TIMEOUT_SECONDS` − 10 s. A
timeout cancels at exactly the timeout; a shutdown comes earlier.

**If the release itself fails** (say Postgres is down too), log it and re-raise anyway. The old
path (stale run, then the reconciler) still recovers it.

**Tests:**
- Cancel `review_pr` mid-review: the run is `queued`, and the re-run claims it and posts once.
- A cancellation at the timeout leaves the run `running`.
- A real arq `Worker` whose job task is cancelled re-runs it and posts once.

**Re-run:** K2. Expect recovery in seconds instead of 1,035 s, with 1 review per PR.

## Fix 2: a GitHub outage fails runs for good (G2) [app/, migration]

**Measured:** 9 of 20 runs failed after a 60 s outage, 16 of 20 after 180 s. All of them gave up
by +84 s, while GitHub was still down. Random 502s didn't fail any run, even at 10%.

**Change (agreed):**
- **Two `review_runs` columns:** `failure_kind` (`transient` or `permanent`, NULL unless failed)
  and `auto_retries` (int, default 0). The migration is hand-written for the CHECK constraint, as
  before; `alembic check` passes.
- **`review_pr` records the kind:**
  - giving up on a `RetryableError` (5xx, timeout, 429, rate limit): `transient`;
  - `NonRetryableError` (other 4xx, a rejected Groq key): `permanent`;
  - an unexpected exception (a bug): `permanent`, because retrying won't fix it.
- **The reconciler's `should_enqueue`:** a `failed` run gets a job when it's `transient`, has
  `auto_retries < 3`, and failed long enough ago: 5, 10, then 20 minutes. The reconciler bumps
  `auto_retries` in the same statement that selects the run, so two reconciler cycles can't both
  count the same retry. `permanent` runs stay failed; a new delivery still retries any failed run,
  as now.
- **Why these numbers:** in G2 every job had given up within about 1.5 minutes of the outage
  starting. The reconciler runs every 5 minutes, so a shorter wait would just be rounded up. With
  3 retries at 5, 10 and 20 minutes, an outage of up to about 40 minutes recovers. A longer
  incident leaves the run failed until the next push, and the 24-hour lookback still bounds it.

**Tests:**
- Each error class stores the right `failure_kind`.
- `should_enqueue`: transient with retries left and old enough → yes; too recent → no;
  `auto_retries = 3` → no; permanent → no.
- Two reconciler cycles racing over the same failed run bump `auto_retries` once.
- Migration upgrade, downgrade and upgrade again.

**Load-test change [loadtest/]:** `is_settled` counts a failed run as settled only if it's
`permanent` or out of automatic retries. G2's wait goes up to 40 minutes.

**Re-run:** G2 with 60 s and 180 s outages. Expect 20 of 20 reviewed, 0 failed, and recovery
within one or two reconciler cycles after the outage, about 5–10 minutes.

## Fix 3: lanes give up early in a Groq 429 storm (Q2) [app/]

**Measured:** with every call answered 429 and `retry-after: 30` for 120 s, `GroqClient` stopped
after 3 attempts (~60 s), with 240 s of the lane deadline left. The 2 PRs in flight got reviews
with no lane output, and their runs are `completed`, so nothing retries them. With 30% random
429s (Q1), 4 of 90 lane calls failed and 2 of 30 reviews lost their bug.

**Change, in two parts:**
- **(a) `app/llm/groq.py` `_post`: 429s stop using up attempts.** The 3 attempts stay for 5xx and
  connection errors. A 429 waits `retry-after` (at least 1 s, so a `retry-after: 0` can't spin)
  and asks again, until waiting would pass the call's deadline. Then it raises
  `DeadlineExceeded` as today, and the chunk is listed as skipped for time. This covers lanes and
  the agent alike; the agent's per-finding time already leaves waits out.
- **(b) `app/worker.py`: don't post a review when no lane produced anything.** If no lane call
  succeeded or came from the cache, and calls failed or ran out of time, raise `RetryableError`
  instead of posting a review that reviewed nothing. That's the same rule as today's "nothing
  reviewed under the daily budget", applied to every cause. The job then retries with backoff,
  and if its tries run out, fix 2 makes it a transient failure the reconciler retries.

**Tests:**
- 429s past 3 attempts succeed within the deadline.
- A 429 past the deadline raises `DeadlineExceeded`.
- `retry-after: 0` waits 1 s.
- 5xx still stops at 3 attempts.
- A run where every lane call failed raises `RetryableError` and posts nothing; a run where one
  lane call succeeded still posts.

**Re-run:** Q2. Expect 10 of 10 reviews with their comment, and lane calls waiting out the
120 s storm. Q1: expect 0 failed lane calls and 30 of 30 with their comment.

**Timing:** fix 3 changes how real Groq 429s are handled, so eval runs after it aren't strictly
comparable to earlier ones on latency and failed calls. Agent decisions don't change. I'd land it
after the remaining eval runs finish (agent-v2's two traps, v3, held-out). Fixes 1 and 2 don't
touch any code the evals run.

## The per-installation concurrency limit (spec 5.3): N1, then your call

The rule agreed in `docs/plans/phase-6.md` has two parts. One can be answered now; the other needs
N1.
- **GitHub rate limits: not a reason.** A review makes 7.2 GitHub requests. At the fake-Groq
  throughput (76 reviews/min) one installation would pass 5,000/hour, but G4 showed the client
  already handles that: it defers until the reset, with 0 403s and 0 failed runs. With real Groq
  (about 5 agent-checked PRs a day) it's a few dozen requests a day.
- **Fairness: needs N1.** I'll add a `noisy_neighbour` scenario to `loadtest/scenarios.py` and an
  N1 driver: installation A sends 100 PRs, and 10 s later installation B sends 1. It measures
  B's wait in reviews waited behind, compared with B alone. This is harness code only, about 5
  minutes to run. arq's queue is FIFO, so I expect B to wait behind all 100.
- **Then the rule:** if B waits behind more than `max_jobs` reviews, a limit is worth building.
  The caveat stays: with real Groq, A's burst uses up the shared daily token budget long before
  queue order matters. The outcome is one of:
  1. build the Redis semaphore;
  2. don't build it, and record why;
  3. a per-installation share of the daily budget instead.

  2 and 3 change the spec, so you decide.

## Not changing (answered by the runs; recorded in decisions.md)
- A Redis-shared Groq limiter (Q3), GitHub rate-limit state in Redis (G4), a verdict cache (G1:
  1.23 agent calls per review at 10% errors), `max_jobs = 2`.
- Burst enqueue latency (queueing; 56 ms p95 at 10/s).
- SIGKILL recovery in about 15 minutes (K1, K1b: 933 s), accepted in Phase 2a.
- The Phase 5 issue "lane calls aren't retried on `output_parse_failed`" stays with the evals
  work. Fix 3(b) would at least stop it posting a review that reviewed nothing.

## Order
1. N1 (harness only; can run now).
2. Fix 1, then K2.
3. Fix 2 with the migration, then G2 at 60 s and 180 s.
4. Fix 3 after the evals finish, then Q1 and Q2.

Stop for review and commit after each fix. Each fix updates its engineering-log entry once its
re-run is in.

## Files
```
app/worker.py                      fix 1; failure_kind (fix 2); nothing-reviewed retry (3b)
app/reconciler.py                  retry transient failures (fix 2)
app/db/models.py, app/db/queries.py, app/db/migrations/versions/<new>.py   fix 2
app/llm/groq.py                    429 handling (3a)
app/pipeline/orchestrator.py       "nothing reviewed" for any cause (3b)
loadtest/failures.py, loadtest/scenarios.py   N1; is_settled for fix 2
tests/                             the tests listed under each fix
```
No new dependencies.
