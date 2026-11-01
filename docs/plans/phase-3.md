# Phase 3 plan: static input, cache, budgets, cost, quality

Approved 2026-11-01. Split into 3a (cost and quality) and 3b (static tools), with a stop for
review and commit after each.

**Done when** (from SPEC.md): pushing a second commit only re-reviews changed chunks (tested),
and cost is stored per run.

## 3a: cost and quality

### Hash cache (spec 5.4)
- **Key:** sha256 of repo ID, lane, the lane's prompt text, model, file path, and the chunk's
  content *without line numbers* (3b adds the static-tool input).
  - Line numbers are left out, so a hunk that moved still hits the cache.
  - Repo ID keeps findings (which can quote code) from being shared across repos.
  - Model and prompt text: a different model or prompt never reuses another's answers.
- **Value:** the lane's findings with chunk-relative line numbers, plus the tokens the original
  call cost. On a hit, findings are shifted to the chunk's current start line.
- **Stored in Postgres** (`chunk_cache`), not Redis, because Render Key Value loses data on
  restart.
- **Written after every successful lane call,** so retries, stale-run takeovers and runs that hit
  the deadline reuse what's already done. Failed calls aren't cached; empty results are.

### Budgets
- **Per PR:** `PR_TOKEN_BUDGET = 50_000` tokens of real LLM calls (cache hits are free).
- **Daily:** `DAILY_TOKEN_BUDGET = 180_000` tokens per model per UTC day (90% of Groq's 200K),
  counted in Postgres (`daily_token_usage`) with an atomic upsert after every call.
- **Order when either budget runs short:** source, then tests, then docs; within a group, chunks
  with more added lines first.
- **Daily budget used up:**
  - **Something was reviewed:** post it, listing the calls not run and why.
  - **Nothing could be reviewed:** release the run and defer the job to just after UTC midnight.
- **Headroom:** the 20K tokens/day left on each model won't cover the Phase 4 agent. Phase 4 must
  decide how the daily budget is split between the pipeline and the agent.

### Cost per run
- **Prices:** code constants in `app/llm/pricing.py`, with the source and date:
  - `gpt-oss-120b`: $0.15 in / $0.60 out per million tokens
  - `gpt-oss-20b`: $0.075 in / $0.30 out per million tokens
- **Stored:** cost is summed per call into `review_runs.cost_usd`, and shown as one line in the
  review summary.
- **New counters on `review_runs`:**
  - `llm_calls` and `cached_calls`
  - `tokens_saved`: the original token cost of every cached call reused, for Phase 5's "the
    cache saved X% of tokens on re-pushes"

### Prompts v2
- `prompts/v2/`, with v1 kept so Phase 5 can compare them.
- The narrow-range rule becomes explicit, with the `average()` example: report line 5, not
  lines 1–5.
- **Check:** a real-Groq run on PR #4's code puts the division by zero on a single line.

### Cross-lane dedupe
- Overlapping lines in the same file: keep the most severe, regardless of lane or category.
- On a severity tie, lane order decides: correctness, then security, then maintainability.

### Models
- Correctness and security stay on `GROQ_MODEL` (`gpt-oss-120b`).
- Maintainability moves to `GROQ_SMALL_MODEL` (`gpt-oss-20b`). That takes about a third of the
  calls off the bottleneck model (about 1.5× throughput), and keeps the stronger model on the
  lanes that matter most.

## 3b: static tools

### Ruff (on by default)
- **How it runs:** `ruff check --isolated --select F,B,UP,SIM`, once per reviewed `.py` file,
  with the file on stdin. `--isolated` means our rules only; the repo's config is never read.
- **What gets kept:** only findings on lines added in the PR.
- **Which lane sees them:** `F` and `B` go to correctness; `UP` and `SIM` go to maintainability.
- **How lanes see them:** as hints inside the untrusted block, with a new prompt version telling
  the model to verify them, not repeat them.
- **Failure:** a Ruff failure never fails the review.
- **Cache:** the tool input becomes part of the cache key.
- **Packaging:** `ruff` moves to the runtime dependencies.

### Semgrep (off by default)
- **The flag:** `SEMGREP_ENABLED=false`.
- **How it runs:** our own rules file (about 6 Python security rules), with findings going to the
  security lane.
- **Packaging:** an optional `semgrep` dependency group, plus a Docker build argument
  `WITH_SEMGREP=false`.

### Not doing
gitleaks: a Go binary to pin in the Dockerfile. The security lane plus a Semgrep secrets rule
cover the obvious cases. Revisit if Phase 5 shows secrets being missed.
