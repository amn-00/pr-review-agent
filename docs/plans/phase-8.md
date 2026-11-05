# Phase 8 plan: deploy on Render free, and the README

Approved 2026-11-05, with the decisions at the end. Two parts:
- **8a, the README:** drafted now. It touches no code. Eval numbers are `TBD` until the final
  runs, and the deploy steps are marked as unverified until 8b has run.
- **8b, the deploy:** waits until the evals finish, because it changes `app/main.py` and
  `app/config.py`.

**`app/` and `evals/` freeze:** the evals are running, so 8a changes only `README.md` and this
plan.

**Done when** (from SPEC.md, Phase 8):
- The API and the worker run as one Render free web service, with Neon Postgres and Render Key
  Value. A real PR on the test repo gets its review from the deployed service.
- The README has an architecture diagram, setup steps, eval results, load and failure-test
  results, and known limits. Every `TBD` is filled in from `evals/results/` and
  `loadtest/results/report.md`.
- The SPEC also lists a short demo GIF. You record it after the deploy (see the questions at
  the end).

## Order after the evals finish
1. **Phase 6c fix 3** (Groq 429s), then Q1 and Q2 again. Without it, a 429 storm on the real
   free tier posts reviews with lanes missing, and those runs never retry.
2. **Fill in the README's eval numbers** from the final reports.
3. **8b, the deploy** (below).
4. **Fill in the README's deploy facts:** the URL, memory on Render, and wake-up time.

Stop for review and commit after each.

## 8a: README [no app/]

**Readers:** recruiters and hiring managers for AI-agent and backend roles. They skim the top,
then read one or two sections. So the first screen says what it is and gives the headline
numbers. The details stay in `docs/`, and the README links to them.

**Sections, in order:**
1. **What it does, and the problem.** LLM reviewers are noisy. Breadth comes from parallel lanes,
   precision from an agent that checks each finding against the code. Headline numbers: catch
   rate and false positives (`TBD`), and 0 duplicate reviews under load and failure tests.
2. **Architecture.** A Mermaid diagram of the system as built, not SPEC section 3's plan:
   gitleaks, ESLint and lethe are left out, the reconciler is added, and Render's single process is
   shown. Then a short "why it's built this way" list: three-layer dedupe, the run marker, the
   atomic claim, the reconciler, and the Postgres cache.
3. **How the agent works.** The loop, the tools, the stopping rules, the verdict as a tool, the
   evidence quote rule, and the untrusted-content wrapping. The v1 → v2 → v3 story: what each eval
   showed and what changed.
4. **Eval results.** The method and its caveats first (synthetic set, tuned on it, held-out
   cases). Then a `TBD` table: pipeline only vs pipeline + agent v3 on the 23 cases, and agent
   v1 vs v3 on the 4 held-out cases.
5. **Load and failure tests.** From `loadtest/results/report.md`. It says up front that Groq
   and GitHub are fakes. A table with one row per failure and what happened, including the before
   and after numbers for K2 and G2. Q1 and Q2 are `TBD` until fix 3 is in.
6. **Setup.** Run it locally (compose plus smee, linked to `docs/github-app-setup.md`), run the
   tests, and deploy to Render (a short summary, linked to `docs/deploy.md` from 8b).
7. **Known limits.** Free-tier throughput, Render sleep, Redis without persistence, SIGKILL
   recovery in about 15 minutes, no per-installation limit, Python-only evals and static tools,
   grep-based `find_callers`, and Semgrep off in prod.
8. **Repo map and further reading.** Short.

**Placeholders:** `TBD` in visible text, and `<!-- TBD: ... -->` for notes on what fills the
gap. `grep TBD README.md` must return nothing before Phase 8 is done.

## 8b: deploy [app/: after the evals]

### The worker inside the API process (as decided before Phase 0)
- **`RUN_WORKER_IN_API` setting** (`bool`, default `false`). Locally, compose keeps the API and
  the worker as separate containers, and the load-test stack is unchanged. On Render it's `true`.
  Rejected: a second entrypoint module (`app.prod:app`), because it would be one more way to start
  the app to explain.
- **`app/main.py` lifespan:** when the setting is on, build the worker with
  `create_worker(WorkerSettings, handle_signals=False)` and run `worker.async_run()` as a
  background task. arq must not install its own SIGTERM handler: uvicorn owns the signals, and two
  handlers would fight. The prod settings and functions stay exactly what `arq` runs locally, and
  the Phase 6 test that compares them still holds.
- **Shutdown:** Render sends SIGTERM on a deploy and on spin-down. Uvicorn then runs the lifespan
  shutdown, which stops the worker the way arq's `handle_sig` does. In-flight jobs are cancelled,
  so Phase 6c fix 1 hands their runs back as `queued`, and arq re-queues them in Redis for the
  next start.
  - **To check while building:** with `handle_signals=False`, `Worker.close()` calls
    `handle_sig(signal.SIGUSR1)`, and Windows has no `SIGUSR1`. Our tests run on Windows locally,
    so the shutdown may need to call the steps itself instead of `close()`. I'll decide that when I
    can see it.
  - **Timing:** Render waits 30 s by default before SIGKILL. That's plenty: K2-fixed released the
    runs right after the stop.
- **If the worker task crashes:** log it and exit the process, so Render restarts the service.
  Rejected: an API that keeps answering webhooks while nothing processes them.

### Migrations: in the start command
`sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT"`. As far as I
know, Render runs pre-deploy commands only on paid instances (to confirm at deploy time). There
is one instance, so two processes can't race to migrate. If a migration fails, the new instance
never passes its health check, and Render keeps the old deploy serving. This replaces compose's
one-shot `migrate` service only in prod.

### `render.yaml` (Blueprint)
- **Web service:**
  - `runtime: docker`, `plan: free`, `healthCheckPath: /health`;
  - `dockerCommand` as above, `PORT=8000`;
  - `ENVIRONMENT=prod`, `RUN_WORKER_IN_API=true`;
  - `REDIS_URL` from the Key Value service;
  - `DATABASE_URL`, the GitHub App values and `GROQ_API_KEY` as `sync: false`, so they're entered
    in the dashboard and never in the repo.
- **Key Value:**
  - `plan: free` (25 MB, no persistence, as decided before Phase 0);
  - `maxmemoryPolicy: noeviction`, because evicting a queued job would lose it silently, while a
    full Redis fails loudly (webhook 503, then the reconciler);
  - `ipAllowList: []`: reachable only from Render services.
- **Health check: `/health`, not `/health/ready`.** A slow Neon wake-up shouldn't make Render
  restart a healthy process. Readiness stays available for checking by hand.
- **Same region as Neon** (e.g. Render Ohio with Neon `aws-us-east-2`): every claim and cache
  lookup is a round trip.

### Neon
- **The direct endpoint, not `-pooler`.** One long-lived process with SQLAlchemy's small pool
  doesn't need PgBouncer, and Neon's docs say migrations should use a direct connection anyway.
- **URL format:** `postgresql+asyncpg://...?ssl=require`. I expect SQLAlchemy's asyncpg dialect
  to reject the `sslmode` and `channel_binding` that Neon puts in its URLs. I'll check against
  Neon before writing it into the deploy doc.
- **Scale-to-zero:** Neon suspends compute after a few idle minutes. `pool_pre_ping` already
  covers dropped connections. I'll measure the first query after a suspend.

### `docs/deploy.md`
Step by step:
1. Neon project and URL.
2. Blueprint from the repo.
3. Secrets in the dashboard (the private key with `\n` escaped, as in `.env`).
4. The GitHub App's webhook URL set to `https://<service>.onrender.com/webhook`.
5. Checks: `/health`, `/health/ready`, a test PR.

### Tests (`tests/test_worker_in_api.py`)
- **Setting on:** the app's lifespan starts a worker that runs a queued `review_pr` to the end
  (fake GitHub and Groq, as in the existing worker tests).
- **Setting off:** no worker starts.
- **Shutdown with a job in flight:** the run ends `queued` and the job is back in arq's queue.
  This is the Render deploy and spin-down path.
- **The worker task crashes:** it's logged and the process is told to exit.

### Checks on the deployed service (results go into the README and the engineering log)
- **A test PR:** reviewed once, inline comments posted. Record wall time and Groq waits.
- **Peak memory** from Render's metrics during a review, against 512 MB. Locally on the fakes:
  about 70 MB for the worker and 67 MB for the API, in separate processes.
- **CPU:** Render free has 0.1 CPU. Time Ruff and the tarball extraction (1.3 s on 16 cores
  locally), since the agent's 120 s per finding counts tool time.
- **A redeploy during a review:** the review is still posted, once.
- **A push while the service sleeps:** a review still arrives, from the waking webhook or the
  startup reconcile. Record how long it takes.

### decisions.md entries (8b)
- The worker in the API process: the flag, `handle_signals=False`, the shutdown path, and the
  crash rule.
- Migrations in the start command.
- Key Value `noeviction`.
- `/health` as the health check.
- The Neon direct endpoint and URL format.
- The shared Groq key (below).

## Known limits the deploy adds (for the README)
- **Render sleeps after 15 minutes without inbound traffic** and takes about a minute to wake.
  GitHub gives up on a webhook after 10 s and doesn't retry, so the webhook that wakes the service
  may count as failed on GitHub's side. The startup reconcile picks the PR up. The 5-minute cron
  doesn't run while the service sleeps (as recorded in Phase 2b).
- **One Groq key, two budget counters.** If evals run after the deploy, prod's
  `daily_token_usage` (in Neon) and the eval database's can't see each other. Only Groq's own
  daily limit stops both, and a review hitting it is deferred to midnight UTC. Avoid running
  evals and prod on the same day, or give prod its own Groq account.
- **750 free instance hours a month** per Render workspace, about one service around the clock.

## Not changing
- **No keep-alive pinger** (an external cron hitting `/health`), as decided at approval. It
  would hide the cold start rather than handle it, and the reconciler already handles it.
- **No runs list or dashboard** (SPEC non-goal).

## Files
```
8a  README.md                          the draft
    docs/plans/phase-8.md              this plan
8b  app/main.py                        worker in the lifespan when RUN_WORKER_IN_API is on
    app/config.py                      run_worker_in_api
    render.yaml                        Blueprint: web service + Key Value
    .env.example                       RUN_WORKER_IN_API, documented
    docs/deploy.md                     deploy steps
    docs/decisions.md                  Phase 8 entries
    docs/engineering-log.md            anything the deploy turns up
    tests/test_worker_in_api.py        the tests above
```
No new dependencies. The Dockerfile doesn't change: Render sets `PORT`, and the start command
reads it.

## Decided at approval (2026-11-05)
1. **Fix 3 lands before the deploy** (see "Order").
2. **The existing GitHub App's webhook URL points at Render.** After that, local dev with smee no
   longer gets webhooks. A separate dev App gets created only if it's needed.
3. **No keep-alive ping.** The cold start is documented as a known limit in the README.
4. **The demo GIF** is recorded by you after the deploy. Until then the README keeps a visible
   placeholder.

**The README's top section stands on its own for a 30-second skim:** the one-line pitch, the
GIF slot and 4 headline numbers (false positives and catch rate, 0 duplicates, outage recovery,
$0 and the cost per PR). Everything below it is for readers who want more.
