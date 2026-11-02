# Phase 6 plan: load and failure tests

Approved 2026-11-02. Split into 6a (harness and load test), 6b (failure tests) and 6c
(decisions and any fixes), with a stop for review and commit after each. The harness is built
first, while agent-v2 runs; the measured runs and the fixes wait until it finishes.

Agreed at approval: an `asyncio` + `httpx` load script (no Locust or k6), and the real 900 s
job timeout in the kill tests. The fixes for the two expected gaps are chosen up front (see K2
and G1/G2), and applied only once the tests confirm the gaps.

**Done when** (from SPEC.md): a load test with 100–500 simulated webhooks reports throughput,
p95 enqueue latency, p95 review time and duplicate reviews (0); failure tests cover killing a
worker mid-job, Groq 429s, GitHub 5xx and a Redis restart. Results are in `loadtest/results/`.

**`app/` freeze:** agent-v2 is running, so `app/` isn't changed until it finishes. Every item
below is tagged **[no app/]** or **[app/: after agent-v2]**. Only the harness is built while
agent-v2 runs. The measured runs wait too: a Docker load on the same machine would skew the
eval's latency numbers, and the eval would skew ours. Only small smoke runs happen before then.

## What the numbers mean
Real throughput is set by Groq's quota (8K tokens/minute, about 5 agent PRs a day; Phase 5
measured it). With fake Groq and fake GitHub, these tests measure the rest of the system: the
webhook, the queue, dedupe, the claim, Postgres, the worker's own overhead, and recovery. The
report says so up front, so the README never presents "N reviews/minute" as real capacity.

## The harness [no app/]

### An isolated stack: `loadtest/docker-compose.yml`
- **Its own compose project and host ports**, so the dev stack and agent-v2's eval DB are never
  touched.
- **Services:**
  - `postgres`: database `pr_review_load`
  - `redis`: `--save "" --appendonly no`, so a restart loses data like Render Key Value
  - `migrate`, and the real `api` (unchanged `app.main`)
  - `worker`: scalable (`--scale worker=2`)
  - `fakes`
- **Workers run in Linux containers.** arq can't install signal handlers on Windows, so the
  SIGTERM and SIGKILL tests only mean something there.
- **The image is the existing one.** `loadtest/` is mounted into the containers, so the
  Dockerfile doesn't change.
- **Fake secrets only, from `loadtest/.env.loadtest`. The real `.env` is never loaded.** A
  call misrouted to the real Groq or GitHub would have no valid credentials. The worker's
  transport also refuses any host it doesn't rewrite. agent-v2's quota is safe either way.

### Fakes: `loadtest/fakes.py`, one FastAPI app
- **Fake GitHub** serves every endpoint the job and the reconciler call:
  - installation token, `GET /app`
  - PR, files (patches in GitHub's `@@` format, reusing the evals' difflib helper if it fits),
    contents
  - reviews (list and create), tarball (with the codeload-style redirect), check runs
  - installations, installation repos, open PRs with `updated_at`

  It sends `X-RateLimit-*` headers, counting requests per installation per hour (limit
  configurable, 5000 by default).
- **Fake Groq**, at `/openai/v1/chat/completions`:
  - **Lane calls:** valid strict output. The correctness lane returns one finding on a known
    added line; the other lanes return none.
  - **Agent calls:** `submit_verdict` `confirmed`, with evidence on that line, so the evidence
    check passes. Every review posts one inline comment.
  - **Latency:** configurable per call.
  - **Usage:** reported as 1 token in and 1 out, so the daily budgets never bind. Those budget
    paths are already tested; here they would only stop the run.
- **Admin endpoints (`/_admin/...`):**
  - set a PR's state (a "push" changes its head before the webhook is sent, as on GitHub)
  - set faults (below)
  - read the records: every request with time, installation, PR and SHA; every review created;
    the first request per (PR, SHA), which marks when its job started
- **Time:** the fakes and the load generator run on the same machine and both record
  wall-clock time, so "webhook sent → review created" needs no DB timestamps.

### The worker: `loadtest/worker.py`
- `LoadTestWorkerSettings(WorkerSettings)` with its own `on_startup`. It builds the real
  `GitHubClient` and `GroqClient` on `httpx` clients aimed at the fakes:
  - **GitHub:** the fakes as `base_url`, with the App key generated at startup.
  - **Groq:** a small transport that rewrites `api.groq.com` to the fakes. `GROQ_URL` is absolute.
- Everything else is prod code: the job, retries, claim, marker check, reconciler cron,
  `max_jobs`, `job_timeout`.
- Each worker adds an `X-Loadtest-Worker: <hostname>` header to its requests. That shows which
  container holds which job, so a kill hits a worker with jobs in flight.

### The load generator: `loadtest/run.py`, plain `asyncio` + `httpx`
- **Sends signed webhooks** (real HMAC) to the API, records send time, status and latency, and
  writes one JSON file per scenario run.
- **Why not Locust or k6** (both listed in SPEC section 4), with no new dependency: the test
  needs four things a load tool doesn't help with:
  - match each webhook to its posted review
  - change the fake's PR state between sends
  - trigger faults and container kills at exact moments
  - check the expected outcome of every PR

  500 requests don't need a load tool. If you'd rather have Locust anyway, say so and I'll
  plan it.

### The webhook mix
Spread over 5 installations × 4 repos, except in the noisy-neighbour scenario.

| Share | Event | Expected |
|---|---|---|
| ~70% | `opened`, new PR | 202, one review |
| ~10% | `synchronize`: a new head on an existing PR, sent soon after | newest head reviewed once; older head reviewed at most once or `skipped` |
| ~10% | exact replay (same delivery ID) | 200 `duplicate`, nothing enqueued |
| ~5% | new delivery, same head (e.g. `reopened`) | no second review |
| ~5% | ignored (draft, `closed`, another event) | 200 `ignored` |

### Correctness checks, for every scenario
- **Duplicates:** reviews created per (repo, PR, SHA) > 1. Must be 0.
- **Lost:** the newest head of an open PR with no review at the end. Must be 0, once recovery
  has had time.
- **Stale posts:** a review posted for a head that had already moved on before the job's
  second `_moved_on` check. Expected 0; it counts as a bug.

### Metrics
- **Enqueue latency:** webhook POST → response, p50/p95/max. Target p95 < 200 ms (spec 5.1),
  measured through Docker's port mapping on Windows. The report says so.
- **Review time:** webhook sent → review created, p50/p95/max. Split into queue wait (sent →
  the job's first GitHub call) and work time.
- **Throughput:** webhooks accepted per second; reviews completed per minute over the steady
  part of the run.
- **GitHub requests per review**, from the fake's log. This answers the rate-limit question
  below.
- **Peak memory** of the api and worker containers (`docker stats` sampled every 2 s), against
  the 512 MB target.

### Results
- **One JSON per scenario run** in `loadtest/results/`: the config, the environment (CPU,
  worker count, fake latencies, git SHA), every webhook and review record, and the metrics.
- **`loadtest/report.py`** builds `loadtest/results/report.md` from the JSON alone, like
  `evals.report`. JSON and report are committed; the load DB is not.

## 6a: load test [no app/]

Fake Groq at 0.5 s per call (a fast real Groq call, with no quota waits).

| Run | Webhooks | Shape | Workers |
|---|---|---|---|
| L1 | 100 | burst (20 concurrent senders) | 1 |
| L2 | 250 | burst | 1 |
| L3 | 500 | burst | 1 |
| L4 | 500 | paced, 10/s | 1 |
| L5 | 500 | burst | 2 |

1 worker with `max_jobs = 2` is the prod shape. L5 shows whether a second worker scales or
just moves the bottleneck to Postgres or the fakes; the fakes' own latency is recorded to
check. A smoke run (20 webhooks) is the only run while agent-v2 is going.

## 6b: failure tests [harness no app/; fixes app/: after agent-v2]
Each test runs on 10–100 PRs with fake Groq slowed (about 30 s per review), so kills land
mid-job. Recovery time = fault (or fault ended) → last affected review posted. Duplicates must be
0 in every test.

### Killing a worker
| Test | How | What I expect from reading the code |
|---|---|---|
| K1 SIGKILL | `docker kill` on a worker with 2 jobs in flight; the other worker keeps running | Queued jobs flow to the survivor at once. The 2 in-flight jobs come back only when arq's in-progress key expires: ~910 s (`job_timeout + 10`). Then `claim_run` takes over the stale run. Recovery ~15 min, as decisions.md accepted. |
| K1b SIGKILL, 1 worker restarted | kill, then start it again right away (like Render restarting the process) | Same ~15 min. The startup reconcile sees a fresh `running` run and leaves it. |
| K2 SIGTERM | `docker stop` (graceful) | **Likely gap.** arq cancels the job and queues it to run again immediately ("cancelled, will be run again"). `review_pr` catches only `Exception`, so `CancelledError` leaves the run `running` with a fresh `started_at`. The immediate re-run fails `claim_run` and returns. The review then comes only from the reconciler, once the run is stale (900 s) plus up to one 5-minute cycle: **15–20 min, for every deploy or restart.** |
| K3 kill after the post | the fake stores the review, then holds the response 30 s; the harness SIGKILLs that worker during the hold | The re-run (~910 s later) finds the run marker and posts nothing. 1 review, run `completed`. |

**The fix, if K2 confirms it (agreed) [app/: after agent-v2]:** on `CancelledError`,
`review_pr` releases the run (back to `queued`) and re-raises, so arq's immediate re-run claims
it. Recovery should drop from ~15 min to seconds; K2 runs again to check. Tests: cancel
mid-review leaves the run `queued`; the re-run posts once.

### Groq 429
| Test | How | Expect |
|---|---|---|
| Q1 | 30% of calls get 429 with `retry-after: 2` | All reviews posted. A call fails only after 3 straight 429s (~2.7%), shown as "lane call(s) failed" or as a finding the agent didn't verify. Measured: the failed share and the extra review time. |
| Q2 | Every call gets 429 for 120 s, `retry-after: 30` | Reviews still post within the 300 s lane deadline, with skipped chunks listed if the deadline hits. No job fails. |
| Q3 | The fake enforces 8K tokens/minute per model (estimated from request size), with Groq's headers. 10 PRs, 1 worker vs 2 | One process should see ~0 429s, because the client waits on the headers. Two processes each keep their own limiter, so 429s may appear. This answers decisions.md's "Phase 6 will show whether a Redis limiter is needed". Prod runs one process, so likely not needed; the number decides. |

### GitHub 5xx
| Test | How | Expect |
|---|---|---|
| G1 | 2% and 10% of GitHub calls get 502 | **To measure:** any 5xx restarts the whole job (about 10 GitHub calls), and there are 5 tries. At 10%, a try succeeds ~35% of the time, so ~1% of runs may end `failed`. Lane calls come from the cache on retry; the agent re-runs. |
| G2 | GitHub returns 503 on every call for 60 s, then for 180 s | **Likely gap:** backoff 5, 10, 20, 40 s (jittered to 50–100%) uses up the 5 tries in ~40–75 s. Jobs caught in a 180 s outage end `failed`. The reconciler skips `failed` runs, so those PRs aren't reviewed until the next push. |
| G3 | `create_review` stores the review, then answers 502 | The retry finds the marker: 1 review. Counts how many agent calls a retry repeats (decisions.md's verdict-cache question). Expected 0 here, because the marker check runs before any LLM call. |
| G4 | Per-installation limit set to 200/hour | Jobs defer until the reset (`MIN_RATE_LIMIT_REMAINING`) and none fail. Answers the "GitHub rate-limit state in Redis?" question from Phase 1. |

**The fix, if G1/G2 confirm it (agreed) [app/: after agent-v2]:**
- **Record the kind of failure:** a failed run records whether it was transient (5xx, timeout,
  429, i.e. it gave up on a `RetryableError`) or permanent (other 4xx, `NonRetryableError`).
- **The reconciler retries transient failures,** with backoff and a maximum attempt count.
- **Permanent failures stay `failed`;** only a new delivery retries them, as now.

The backoff and attempt numbers are picked from G2's results. G1/G2 run again to check.

### Redis restart (no persistence)
- **R1:** 100 webhooks sent; while ~half are still queued, `docker compose restart redis`. The
  generator keeps sending during the outage.
- **Expected:**
  - The API answers 503 while Redis is down (recorded).
  - Queued jobs and delivery keys are lost.
  - Jobs in flight finish against Postgres.
  - The reconciler re-enqueues every lost PR: "no run" or `queued`. That happens at worker
    startup if the worker exits on the lost connection, or else at the next 5-minute cycle.
    Which one happens is recorded.
- **Recovery time:** Redis back → last lost PR reviewed. Expected ≤ 5 min plus the backlog.
- **R2:** after R1, replay every delivery. The dedupe keys are gone, so all are enqueued.
  Postgres's unique run plus `completed` must stop them all: 0 new reviews.

## 6c: decisions [docs no app/; any fix app/: after agent-v2]

### The per-installation concurrency limit (spec 5.3)
- **N1 noisy neighbour:** installation A sends 100 PRs; 10 s later installation B sends 1.
  B's queue wait, in reviews it waited behind, is compared to B alone. arq's queue is FIFO, so
  B probably waits behind all 100.
- **The rule, agreed before the run:**
  - **Fairness:** a limit is worth building if B waits behind more than `max_jobs` reviews.
    A cap of 1 job per installation would bound B's wait to about one review.
  - **GitHub limits:** it's also needed if one installation at the measured throughput could
    exceed 5000 GitHub requests/hour (requests per review × reviews per hour).
- **The caveat the report must state:** with real Groq, A's 100 PRs use up the shared daily
  token budget long before concurrency matters, and B is deferred to midnight either way. A
  concurrency cap fixes the queue order, not that. A per-installation share of the daily budget
  is the lever for budget starvation.
- **The outcome is one of:**
  1. build the Redis semaphore, as spec and decisions.md planned (if over the limit,
     `Retry(defer)` to the back of the queue) [app/];
  2. don't build it, and record why in decisions.md;
  3. propose a per-installation budget share instead.

  2 or 3 changes the spec, so it's your call once the numbers are in.

### The other questions decisions.md deferred to Phase 6
| Question | Answered by |
|---|---|
| GitHub rate-limit state in Redis? | G4, plus requests per review |
| Groq limiter in Redis? | Q3 |
| Verdict cache for retries after the agent? | G1/G3: agent calls repeated per retried run |
| `max_jobs = 2` | L1–L5 memory and throughput |

### After the runs
- **decisions.md:** a Phase 6 entry for each answer, and for each fix with its before/after
  numbers.
- **Fixes [app/: after agent-v2]:** each gets failure-path tests, then the failure test that
  found the gap runs again.

## Files
```
loadtest/docker-compose.yml   isolated stack
loadtest/.env.loadtest        fake settings only (no real secrets)
loadtest/fakes.py             fake GitHub + fake Groq + admin/fault/record endpoints
loadtest/worker.py            WorkerSettings subclass wiring the real clients to the fakes
loadtest/scenarios.py         webhook mixes + expected outcome per PR
loadtest/run.py               CLI: run a scenario, faults, kills, write JSON
loadtest/report.py            JSON -> report.md
loadtest/results/             per-run JSON + report.md
tests/test_loadtest_*.py
```
No new dependencies (FastAPI, uvicorn, httpx, cryptography via PyJWT are already in).

## Tests (pytest, no Docker)
- **Contract:** the real `GitHubClient` and `GroqClient` against the fakes through
  `httpx.ASGITransport`. Every method the job and the reconciler use returns what they expect:
  patches parse, paging works, the tarball extracts. Fake lane output passes `LaneOutput`.
- **One PR end to end:** `review_pr` with the fakes, on the test DB. It posts one review with
  one confirmed comment, so the fake verdict passes the evidence check.
- **Fault switches:** rate, window, accept-then-fail and hold each behave as set.
- **Scenarios:** each mix's expected outcomes are right (replay, same-head, new head, ignored).
- **Report:** percentiles, the duplicate, lost and stale-post counts, and recovery time, on
  hand-built records.
- **Any `app/` fix:** its own failure-path tests (e.g. cancel mid-review releases the run).

## Not doing
- Real Groq or GitHub: no quota, and agent-v2 owns today's quota.
- Postgres outages, and Render sleep/wake. Phase 8 deploys and covers the single-process
  layout (worker inside the API).
- Locust or k6 (see above).
