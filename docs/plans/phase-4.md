# Phase 4 plan: the verification agent

Approved 2026-11-01. Split into 4a (the agent on its own) and 4b (wired into the worker), with a
stop for review and commit after each.

**Done when** (from SPEC.md): a planted false positive gets dropped with evidence, and the agent
never exceeds max steps (tested).

## The constraint that shapes it
Groq's free tier gives gpt-oss-120b 8K tokens per minute. An agent step resends the whole
conversation, so checking one finding (about 4 steps) costs roughly 10–12K tokens: about 1.5
minutes of one model's per-minute quota. In the agent's time window that's about 4 checked
findings per PR. Parallel agents don't help when the per-minute quota is the bottleneck, so the
agent is kept small: short tool results, a token cap per finding, most severe findings first.

## 4a: the agent on its own
The repo snapshot and the four tools, Groq tool calling, the loop with its stopping rules,
verdicts and evidence checks, the `agent_steps` table, and the agent prompt. Tested with scripted
fake Groq replies, plus a real-Groq script on a planted false positive that posts nothing.

## 4b: wired into the worker
The config flag, the daily budget split, findings saved before the agent runs, the summary and
inline comments, and the real-GitHub test that meets "done".

## The loop and a reliable final verdict
Groq can't combine strict structured outputs with tool use. `tool_choice` accepts `none`, `auto`,
`required` or one named function, and gpt-oss models don't support parallel tool calls.
- **The verdict is a tool:** `submit_verdict(verdict, reason, evidence[])`, next to the four read
  tools. `verdict` is an enum (confirmed, dropped, uncertain); evidence is a list of
  `{path, line, why}`.
- **`tool_choice: "required"` on every step,** so the model never answers in prose and a verdict
  is never guessed from free text.
- **The last allowed step forces `submit_verdict`** by naming it in `tool_choice`: at the step
  limit, or when the call after this one wouldn't fit the token cap.
- **Arguments are checked with Pydantic.** Invalid ones go back to the model as the tool result,
  and use up a step.
- **Groq's `tool_use_failed` 400** (a broken tool call) uses up a step and the loop asks again.
  Any other `LLMError` ends the finding as `uncertain` ("agent error").
- **Rejected:** gathering with tools, then one strict JSON call without tools. It's an extra call
  that resends the whole conversation. Kept as a fallback if Phase 5 shows many runs without a
  verdict.

`GroqClient.complete_tools(...)` shares the retry, rate-limit and deadline handling with
`complete_json`. The pre-send estimate counts the tool schemas and tool-call arguments.

## Tools
**The repo snapshot** is built once per attempt, only if there's at least one finding to check.
- **Download:** `GET /repos/{repo}/tarball/{sha}`, which redirects to codeload (httpx drops our
  token on that cross-host redirect). Streamed to a temp file, at most 50 MB compressed.
- **Extraction:** in a thread, reading the archive as a stream (`tarfile` mode `r|gz`), never
  `extractall`. Regular files only (no links or devices), top folder stripped, a path guard, at
  most 512 KB per file, binaries skipped (a NUL byte in the first 8 KB), at most 100 MB or 20K
  files in total. Past a limit, the snapshot is marked incomplete and results say so.
- **If it fails** (too big, 404, timeout), the repo tools answer "repository files unavailable"
  and the review continues. The temp folder is deleted when the attempt ends.

| Tool | Behaviour |
|---|---|
| `search_repo(query)` | Plain text, never a regex (no model-chosen pattern that takes forever). At most 200 characters. Up to 15 `path:line: text` matches, lines cut to 200 characters, plus "N more". |
| `read_file(path, start, end)` | Up to 80 numbered lines. Path guard. A missing file or bad range is an error message for the model. |
| `find_callers(symbol)` | Identifier-like symbols only (a dotted name uses its last part). Whole-word, escaped match, leaving out the symbol's own `def`/`class` lines. Up to 15 matches. Grep-based. |
| `get_ci_results()` | `GET /commits/{sha}/check-runs`: name, status, conclusion, title (cut short). Fetched once per attempt. A GitHub error comes back as text. |

Every result is capped at 2,500 characters. A tool never raises: errors become text for the
model. "Checks: Read-only" is already granted on the installation. CI is often still running a
minute after a push; the tool says so.

## Stopping rules
The loop is `for step in range(1, MAX_AGENT_STEPS + 1)`; there is no `while`.

| Rule | Value | What happens |
|---|---|---|
| Max steps (one step = one LLM call) | 6 | Step 6 forces `submit_verdict`. No verdict means `uncertain`. |
| Tokens per finding | 12K | If the call after this one wouldn't fit, the verdict is forced now; if this one doesn't fit, `uncertain`. |
| Time per finding | 120 s | Passed to Groq as the deadline. Running out means `uncertain`. *(Changed in 4b: working time only, rate-limit waits left out; past it the verdict is forced. See decisions.md.)* |
| Repeated identical tool call | | Not run; the model is told "already ran". Still uses a step. |
| Extra tool calls in one reply | | Answered "not run". `parallel_tool_calls: false` is sent too. |

## Verdicts and evidence
- **confirmed:** what the agent read shows the problem is real. **dropped:** it shows there is
  none. **uncertain:** it couldn't tell.
- **Evidence must point at something the agent saw:** lines of the finding's chunk, lines
  `read_file` returned, or lines in search and caller results. `confirmed` or `dropped` without
  such evidence becomes `uncertain`.
- **Posting (4b):** confirmed findings are posted with a "Checked against `path:line`" line (file
  locations only, through `safe()`). Uncertain ones go in a collapsed "Uncertain (not posted)"
  section with the agent's one-line reason. Dropped ones are only counted in the summary; their
  evidence and steps stay in the DB.
- **Not verified** (budget or time ran out before the agent started): `verdict` NULL with a
  reason, in a separate collapsed section, not posted.
- Findings are checked most severe first.

## `agent_steps`
- Columns: `id`, `finding_id` (foreign key, ON DELETE CASCADE), `step_no`, `tool` (NULL when the
  call failed), `args_json` (JSONB), `result_chars`, `result_summary` (first 300 characters),
  `tokens_in`, `tokens_out`, `latency_ms`. Unique `(finding_id, step_no)`.
- New `findings` columns: `verdict_reason`, `evidence` (JSONB).
- **Order (4b):** findings are saved right after aggregate (replacing the attempt's findings and
  their steps), each step is written as it finishes, verdicts and `posted` are updated at the end.
- Agent calls add to the run's usage totals after every call.
- **Known cost:** if posting fails and the job retries, the agent runs again (no verdict cache).

## Prompt injection
- Tool results are wrapped in `BEGIN/END UNTRUSTED TOOL RESULT <id>`, with a new random ID per
  result. The candidate (whose text may repeat injected PR content) and its chunk go in the usual
  untrusted PR block.
- `prompts/agent/v1.md` is fixed, says both blocks are data, and defines verdicts and evidence.
- **No power:** the tools only read this repo's snapshot at this commit and its CI results. The
  verdict only filters: the agent can't add findings or change their text. Worst case, an
  injection hides a real finding (with evidence from code the agent really read) or confirms a
  false one.

## Daily budget split (4b)
- The agent runs on `GROQ_MODEL` (gpt-oss-120b): checking evidence is the precision step.
- `daily_token_usage` gains a `purpose` column (pipeline or agent) in its key.
- `PIPELINE_DAILY_TOKEN_BUDGET = 110_000` and `AGENT_DAILY_TOKEN_BUDGET = 70_000` per model; the
  20K headroom stays. `AGENT_PR_TOKEN_BUDGET = 30_000` per attempt.
- About 5 PRs a day with the agent. The budgets are the same whether the agent is on or off.

## The flag (4b)
`AGENT_ENABLED=true` by default. Off means exactly Phase 3: all aggregated findings posted,
`verdict` NULL. `review_runs.agent_enabled` records the mode. The lane cache doesn't depend on
the flag, so Phase 5 can run both modes on one PR and pay only for the agent the second time.

## Deadlines (4b)
Lanes must finish 300 s after the job starts (was 600 s), the agent by 720 s, and the job timeout
stays 900 s. Findings not reached in time are "not verified (time limit)". The lane deadline is
the same in both modes.

## Checks
- Tests for the failure paths: tarball traversal, links, size caps, binaries, download failure, a
  CI 403 as text, every stopping rule, invalid verdicts, `tool_use_failed`, evidence checks,
  injection wrapping; in 4b, flag off identical to Phase 3, only confirmed posted, both collapsed
  sections, agent budget used up.
- Mutation checks: the forced last step, the evidence check, the path guard.
- Real Groq (4a): an f-string SQL query whose column comes from an allow-list in another file
  should be dropped, citing the allow-list; a real bug next to it should be confirmed.
- Real GitHub (4b): the same pair on a playground PR.
- Container memory during snapshot and search on a medium-sized repo.
