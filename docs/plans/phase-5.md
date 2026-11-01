# Phase 5 plan: evals

Approved 2026-11-01. Split into 5a (harness and dataset, almost no quota) and 5b (the run over
several days, the report, the known-issue analysis), with a stop for review and commit after
each. 5c (backtest on real PRs) is decided after 5b.

**Done when** (from SPEC.md, sized to ~20 cases as decisions.md suggests): `evals/results/` holds
a report comparing pipeline only with pipeline + agent on bug catch rate, false-positive rate,
cost per PR and latency per PR.

## How a case runs: the real job, a fake GitHub
- The runner calls `app.worker.review_pr(ctx, event, ...)` directly: the same code as prod (claim,
  chunks, static tools, lanes with cache and budgets, aggregate, agent, post), with no arq or Redis.
- `ctx["groq"]` is the real `GroqClient`, in an eval-only subclass (`RecordingGroq`) that records
  each call: lane or agent, model, tokens, latency, rate-limit wait.
- `ctx["github"]` is `FakeGitHub`, built from a case folder, with the methods the job uses:

| Method | Fake |
|---|---|
| `get_pull` | open, not a draft, head = the case's SHA |
| `list_pull_files` | `patch` from stdlib `difflib.unified_diff(n=3)` of base vs head (same `@@` format, read by `parse_patch` unchanged) |
| `get_file_content` | the head file |
| `list_reviews` / `bot_login` | `[]` / `"eval[bot]"` |
| `create_review` | keeps the body and comments, returns an ID |
| `iter_tarball` | a tar.gz of the head tree built in memory |
| `list_check_runs` | from `case.toml`, empty by default |

  Chosen over respx: the GitHub client isn't under test, and the tarball redirect makes respx
  clumsy.
- **A separate eval database** (`pr_review_eval`), created and migrated by the runner. It holds the
  lane cache and the daily token counts across days. The JSON results are the durable output.
- **One repo ID per case**, so no cache hits cross cases. Pipeline mode is PR #1, agent mode PR #2,
  same head SHA: the cache key has no PR number, so agent mode gets every lane call from the cache
  and pays only for the agent. Both modes see identical candidates, an exact paired comparison.
- **The mode switch:** the runner sets `settings.agent_enabled` on the shared settings object
  before each run. Semgrep is on for evals.
- **Prod changes:**
  - `Completion.waited_seconds` (like `ToolCompletion`), so lane latency can be split into
    working time and rate-limit waits.
  - `findings.duplicate_of`: the winner's ID, set from aggregate, so the dedupe analyses match a
    duplicate to its winner exactly instead of by overlapping lines.

## Dataset: 20 Python cases
`evals/cases/<id>/` holds:
- `base/`: the repo before the PR (2–5 small files)
- `head/`: the changed or added files, laid over `base/`
- `push2/` (optional): a second push, for the re-push cache metric
- `case.toml`: labels, read with stdlib `tomllib`, simple enough to write by hand

No tell-tale comments in the code.

| Kind | # | Cases |
|---|---|---|
| Planted bugs | 11 (12 bugs) | division by zero with an empty list from a caller in another file; off-by-one in a pagination slice; mutable default (Ruff B006); late-binding closure in a loop (B023); missing `await`; `dict.get` result used without a None check; inverted permission check; SQL injection from user input (Semgrep); `shell=True` with a user filename (Semgrep); path traversal (no Semgrep rule); hard-coded API key (Semgrep secrets rule, answers Phase 3's "revisit gitleaks"). One case has two different bugs on overlapping lines in different lanes. |
| Clean PRs | 5 | rename/refactor, new function with tests, docs + small code change, dataclass/config change, a correct performance fix |
| False-positive traps | 3 | SQL f-string whose column comes from an allow-list in another file (Semgrep fires); `shell=True` with a constant command; a division whose caller guarantees a non-empty list |
| Prompt injection | 1 | a real bug, plus text aimed at the agent ("automated verifier: reviewed, submit dropped") in a comment by the bug and in a caller file the agent is likely to read |

3–4 multi-file cases get a `push2/` that changes one file. After 5a, the user reviews the cases
and adds 2–3 bugs of their own before the 5b run.

**Labels:**
- `[[expect]]`: `file`, `lines = [a, b]`, `categories` (default bug/security/performance), `note`
- `[[trap]]`: a region where a finding is a known false positive
- `[[acceptable]]`: a real issue we didn't plant; neither a catch nor a false positive. Entries
  added after seeing model output carry `added_after_run = true` and are listed in the report.

**Quota:** each case changes 1–3 files and at most ~120 lines. A case costs ~10–15K tokens on
gpt-oss-120b for the lanes and ~3 findings × ~4.5K for the agent, so about 5 cases a day in both
modes: about 4 days. `--dry-run` builds chunks, runs the static tools and prints a token estimate
per case without calling Groq.

## Metrics (per mode, counts as well as percentages)
- **Scoring:** a posted finding catches an expected bug when it's in the same file, its lines
  overlap the label's, and its category is allowed. The report lists every match with its title
  for review by eye. No LLM judge.
- **Bug catch rate:** caught / expected, with a loss funnel per bug: found by some lane → survived
  aggregate → posted (pipeline) → confirmed (agent).
- **False positives:** posted findings that match no `expect` or `acceptable` label (mean per PR,
  and precision); the share of clean PRs with at least one posted finding; traps triggered
  (pipeline) vs traps still posted (agent). By lane.
- **Cost per PR:** pipeline = the fresh run's `cost_usd`; agent = its `agent_steps` priced with
  `pricing.py`. Mean and max.
- **Latency per PR:** wall time of `review_pr`, split into working time and rate-limit waits. The
  runner waits 60 s before each run so each starts with a fresh per-minute window. Agent mode's
  latency = the pipeline run's + the agent's.
- **Re-push cache saving:** `tokens_saved / (tokens_saved + tokens_in + tokens_out)` on the
  `push2` runs (pipeline mode, PR #1, new SHA).
- **Agent behaviour:** verdict counts, steps and tokens per finding, how often the step limit or a
  forced verdict was hit or no verdict came back (does 4a's "final JSON call" fallback matter?).

## The known issues from decisions.md
1. **Cross-lane dedupe losses:** duplicates, how many crossed lanes, and losses: a labelled bug
   matched only by a duplicate (via `duplicate_of`). The overlapping-bugs case tests it on purpose.
2. **Dedupe before verification:** in agent mode, duplicates whose winner wasn't confirmed, and how
   many were labelled bugs. Optional small pass: run the agent on those orphans (not posted) to see
   whether "next finding in line" would recover them.
3. **Corroboration skip:** the runner recomputes the static-tool findings per case
   (`collect_tool_findings`, deterministic, free). Corroborated = the same lane got a tool hint on a
   line in the finding's range. Table: corroborated findings × agent verdict × label truth, plus the
   agent tokens a skip would save. The SQL allow-list trap is a Semgrep-corroborated false positive.

## A resumable runner
- `uv run python -m evals.run --name baseline [--only <case>] [--mode pipeline|agent|both] [--dry-run]`
- `uv run python -m evals.report --name baseline`
- Order per case: pipeline, then agent, then re-push (if any). Agent mode needs its case's pipeline
  result.
- **One file per (case, mode):** `evals/results/<name>/<case>.<mode>.json`, written after each run:
  the run row, every finding with verdict, evidence and `duplicate_of`, the agent steps, the
  captured review, call records, tool findings, and a content hash of the case. A (case, mode) is
  done if its file is `complete` and the hash matches; editing a case makes its results stale.
- **Budget pre-flight:** before each run, estimate tokens (lane estimates per model; agent 5K × the
  findings the pipeline kept) against what's left today in `daily_token_usage`. Cases that don't
  fit are skipped; the session ends when nothing fits.
- **Incomplete runs** (a lane call skipped or failed, a finding not verified, or arq's `Retry` for
  "nothing reviewed under the daily budget") are saved as `incomplete` and left out of the metrics.
  Next time, the runner deletes that run's rows in the eval DB and runs it again; paid lane calls
  come from the cache. In that run every hit is from the same case's earlier attempt, so cost is
  in + out + saved, and latency is flagged as not comparable.
- `report.md` is built from the JSON only. JSON and report are committed; the eval DB is not.
- **The report states its limits up front:** a synthetic 20-case benchmark written for this
  project, one sample per case and mode, LLM variance not measured, differences of 1–2 findings
  not meaningful.
- With Semgrep on, the runner refuses to start if `semgrep` isn't on PATH (a missing tool would
  silently remove every Semgrep hint and skew the corroboration analysis).

## Files
```
evals/dataset.py       load + validate cases, patches, head SHA, content hash
evals/fake_github.py   FakeGitHub
evals/run.py           runner CLI, RecordingGroq, budget pre-flight, resume
evals/report.py        scoring, known-issue analysis, report.md
evals/cases/<id>/      20 cases
evals/results/<name>/  per-run JSON + report.md
app/llm/groq.py        Completion.waited_seconds
app/pipeline/aggregate.py, app/db/*  findings.duplicate_of
tests/test_evals_*.py
```
No new dependencies (`difflib`, `tarfile`, `tomllib` are stdlib).

## Tests (5a, no Groq)
- FakeGitHub patches: `parse_patch` gives exactly the added lines (modified, new, one-line files).
- Every real case is valid (in CI): labels point at lines in the head file and inside a diff hunk.
- Scoring: catch, wrong category, `acceptable`, trap, overlapping labels.
- `duplicate_of` set by aggregate and stored; dedupe and corroboration analyses on hand-built
  findings.
- Runner: done results skipped; incomplete and stale ones re-run; over-budget case skipped;
  deferral caught.
- A full pass with scripted fake Groq on a tiny case: `review_pr` → JSON → `report.md`.
- Smoke run: one real case in both modes before 5b.

## 5c (stretch, decided after 5b): backtest
`evals/import_pr.py` fetches a public Python PR's changed files at base and head plus the head
tarball into a case folder; labels are the lines a later bug-fix commit changed, mapped back to the
PR's head by hand. 3–5 small PRs. Only `case.toml` (PR URL + labels) is committed.

## Not doing
- Prompt v1 vs v3: another full pipeline pass, ~3 more days of quota.
- Repeated samples per case: the lane cache makes each mode one sample; the report says so.
