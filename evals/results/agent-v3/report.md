# Eval report: agent-v3

Generated 2026-11-04 10:42 UTC from `evals/results/agent-v3/`.

**Read this first.**
- This is a synthetic benchmark of 23 small Python cases, written for this project. The
  bugs were planted by the same people who built the reviewer, so it shows how the parts
  behave relative to each other, not how well the reviewer does on real-world PRs.
- One sample per case and mode. The lane cache makes both modes judge exactly the same
  candidates, but the LLM's run-to-run variance is not measured: a rerun could differ.
- With this few cases, a difference of 1–2 findings is not meaningful.
- Latency is on Groq's free tier, where most of it is waiting for the per-minute token limit.
- **Tuned on the test set.** Every agent prompt after v1 was written after reading earlier
  results on these same cases, so its scores here are optimistic. Only cases it was never
  tuned on (`evals/heldout/`) show whether it generalizes.

## Pipeline only vs pipeline + agent

**Scoring rule (applied to all runs):** precision, false positives and traps count **inline comments only**: findings in the categories bug, performance and security that passed aggregation and, with the agent, were confirmed. Style findings (readability, idiom) are collapsed suggestions: counted and listed separately, never scored. Every run is scored this way, including runs made before the rule existed, so numbers in older reports differ.

14 of 23 cases have complete results in all three.

| Metric | Pipeline only | Pipeline + agent (agent-v2, agent v2) | Pipeline + agent (agent v3) |
|---|---|---|---|
| Bugs caught | 12/13 (92%) | 12/13 (92%) | 12/13 (92%) |
| Bugs lost to an uncertain verdict (of which, by the evidence check) | — | 0 (0) | 0 (0) |
| Traps posted | 0/0 | 0/0 | 0/0 |
| Clean PRs with a false positive | 0/2 | 0/2 | 0/2 |
| False positives | 3 (0.21 per PR) | 1 (0.07 per PR) | 1 (0.07 per PR) |
| Inline comments | 19 | 17 | 17 |
| Precision (inline comments) | 84% | 94% | 94% |
| Suggestions (collapsed, not scored) | 3 | 1 | 2 |
| Cost per PR (list prices) | $0.0010 (max $0.0015) | $0.0021 (max $0.0033) | $0.0026 (max $0.0048) |
| Latency per PR | 6 s (max 6 s) | 42 s (max 81 s) | 53 s (max 125 s) |
| Rate-limit waits per PR (summed over parallel calls) | 0 s | 27 s | 45 s |

**Over-correction check, against `agent-v2`:** bugs caught +0, traps posted +0, clean PRs with a false positive +0, false positives +0. This agent wins only if bugs caught don't go down, none of the other three go up, and at least one of the four gets better: **doesn't pass**. With one sample per finding, a change of 1–2 can be noise.

Pipeline + agent cost and latency = the pipeline run's plus the agent's (the agent run got every lane call from the cache). Latency leaves out runs that took more than one attempt. False positives are inline comments that match no planted bug and no `acceptable` label; precision = inline comments matching a planted bug / all inline comments that aren't `acceptable`. A bug lost to an uncertain verdict was reported by a finding that passed aggregation but that the agent ended `uncertain`; the number in brackets is how many of those the evidence check made uncertain (a quote not on its line, or a line the agent wasn't shown).

## Changed verdicts

| Case | Finding | Label | Verdict (agent-v2) | Verdict (this run) |
|---|---|---|---|---|
| bug-01-empty-average | `shop/stats.py:6-7` maintainability/low: Unnecessary blank lines added | false positive | dropped | uncertain: dropped without evidence from code it read (the quote for shop/stats.py:6 isn't on that line): PEP8 recommends exactly two blank lines between top‑level function definitions, so the added blank lines  |
| bug-05-missing-await | `accounts/service.py:8` correctness/high: Missing check for non‑existent user | false positive | dropped | uncertain: dropped without evidence from code it read (the quote for accounts/db.py:8 isn't on that line): get_user never returns None – it attempts to build a dict from the query result, raising TypeError itsel |
| bug-05-missing-await | `accounts/service.py:11` maintainability/low: Return type is a raw dict | false positive | dropped | confirmed: The function returns the raw dict `user` without a typed model, confirming the finding about low readability. |

## Per case

| Case | Kind | Caught (P / A) | FP (P / A) | Cost (P / A) | Wall (P run / A run) | Notes |
|---|---|---|---|---|---|---|
| bug-01-empty-average | bug | 1 / 1 of 1 | 0 / 0 | $0.0013 / $0.0048 | 6 / 115 s |  |
| bug-02-pagination-off-by-one | bug | 1 / 1 of 1 | 0 / 0 | $0.0012 / $0.0017 | 6 / 11 s |  |
| bug-03-mutable-default | bug | 1 / 1 of 1 | 0 / 0 | $0.0013 / $0.0040 | 6 / 53 s |  |
| bug-04-late-binding | bug | 1 / 1 of 1 | 1 / 0 | $0.0010 / $0.0025 | 6 / 60 s |  |
| bug-05-missing-await | bug | 1 / 1 of 1 | 1 / 0 | $0.0009 / $0.0036 | 6 / 120 s |  |
| bug-06-dict-get-none | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0030 | 6 / 110 s |  |
| bug-07-inverted-permission | bug | 1 / 1 of 1 | 0 / 0 | $0.0015 / $0.0038 | 6 / 49 s |  |
| bug-08-sql-injection | bug | 1 / 1 of 2 | 0 / 0 | $0.0008 / $0.0018 | 6 / 7 s |  |
| bug-09-shell-true | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0020 | 6 / 6 s |  |
| bug-10-path-traversal | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0023 | 6 / 64 s |  |
| bug-11-hardcoded-key | bug | 1 / 1 of 1 | 1 / 1 | $0.0014 / $0.0039 | 6 / 52 s |  |
| bug-12-blocking-sleep | bug | 1 / 1 of 1 | 0 / 0 | $0.0007 / $0.0015 | 5 / 7 s |  |
| clean-02-new-function-with-tests | clean | 0 / 0 of 0 | 0 / 0 | $0.0012 / $0.0012 | 6 / 4 s |  |
| clean-05-perf-fix | clean | 0 / 0 of 0 | 0 / 0 | $0.0005 / $0.0005 | 5 / 4 s |  |

## Where each bug was lost

| Case | Bug | A lane found it | Survived aggregate | Posted (P) | Confirmed (A) |
|---|---|---|---|---|---|
| bug-01-empty-average | division by zero for a customer with no paid orders | yes | yes | yes | yes |
| bug-02-pagination-off-by-one | off by one: each page drops its last item | yes | yes | yes | yes |
| bug-03-mutable-default | mutable default set: tags seen for one article are dropped from later ones | yes | yes | yes | yes |
| bug-04-late-binding | the lambda binds the loop variable late: every reminder goes to the last user | yes | yes | yes | yes |
| bug-05-missing-await | save_user is async and isn't awaited: the new name is never saved | yes | yes | yes | yes |
| bug-06-dict-get-none | STAFF.get returns None for an unknown assignee, then subscripting it raises TypeError | yes | yes | yes | yes |
| bug-07-inverted-permission | inverted check: everyone except the owner may edit | yes | yes | yes | yes |
| bug-08-sql-injection | SQL injection: order_ref from the URL is formatted into the query | yes | yes | yes | yes |
| bug-08-sql-injection | fetchone() is None for an unknown order, so [0] raises TypeError | yes | **no** | **no** | **no** |
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | yes | yes | yes | yes |
| bug-10-path-traversal | path traversal: ?file=../../etc/passwd escapes the reports folder | yes | yes | yes | yes |
| bug-11-hardcoded-key | production API key hard-coded in the source | yes | yes | yes | yes |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | yes | yes | yes | yes |

## Matches

Every catch, so the automatic matching can be checked by eye.

| Case | Bug | Mode | Finding |
|---|---|---|---|
| bug-01-empty-average | division by zero for a customer with no paid orders | P | `shop/report.py:7` correctness/high: Potential division by zero when calculating average order value |
| bug-01-empty-average | division by zero for a customer with no paid orders | A | `shop/report.py:7` correctness/high: Potential division by zero when calculating average order value |
| bug-02-pagination-off-by-one | off by one: each page drops its last item | P | `api/pagination.py:6-7` correctness/medium: Slice end off by one, omits last item of page |
| bug-02-pagination-off-by-one | off by one: each page drops its last item | A | `api/pagination.py:6-7` correctness/medium: Slice end off by one, omits last item of page |
| bug-03-mutable-default | mutable default set: tags seen for one article are dropped from later ones | P | `tags/registry.py:7` correctness/high: Mutable default argument for 'seen' leads to shared state |
| bug-03-mutable-default | mutable default set: tags seen for one article are dropped from later ones | A | `tags/registry.py:7` correctness/high: Mutable default argument for 'seen' leads to shared state |
| bug-04-late-binding | the lambda binds the loop variable late: every reminder goes to the last user | P | `notify/reminders.py:11` correctness/high: Late-binding closure captures loop variables |
| bug-04-late-binding | the lambda binds the loop variable late: every reminder goes to the last user | A | `notify/reminders.py:11` correctness/high: Late-binding closure captures loop variables |
| bug-05-missing-await | save_user is async and isn't awaited: the new name is never saved | P | `accounts/service.py:10` correctness/high: Asynchronous save operation not awaited |
| bug-05-missing-await | save_user is async and isn't awaited: the new name is never saved | A | `accounts/service.py:10` correctness/high: Asynchronous save operation not awaited |
| bug-06-dict-get-none | STAFF.get returns None for an unknown assignee, then subscripting it raises TypeError | P | `support/escalate.py:6-7` correctness/high: Potential NoneType access when assignee not found |
| bug-06-dict-get-none | STAFF.get returns None for an unknown assignee, then subscripting it raises TypeError | A | `support/escalate.py:6-7` correctness/high: Potential NoneType access when assignee not found |
| bug-07-inverted-permission | inverted check: everyone except the owner may edit | P | `docs_app/permissions.py:6-7` correctness/high: Incorrect permission check logic |
| bug-07-inverted-permission | inverted check: everyone except the owner may edit | A | `docs_app/permissions.py:6-7` correctness/high: Incorrect permission check logic |
| bug-08-sql-injection | SQL injection: order_ref from the URL is formatted into the query | P | `orders/api.py:12` security/high: SQL injection risk due to string interpolation in query |
| bug-08-sql-injection | SQL injection: order_ref from the URL is formatted into the query | A | `orders/api.py:12` security/high: SQL injection risk due to string interpolation in query |
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | P | `media/thumbnails.py:11` correctness/high: Improper use of shell=True with unescaped file paths |
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | A | `media/thumbnails.py:11` correctness/high: Improper use of shell=True with unescaped file paths |
| bug-10-path-traversal | path traversal: ?file=../../etc/passwd escapes the reports folder | P | `files/download.py:8-9` correctness/high: Potential path traversal allowing access to files outside REPORTS_DIR |
| bug-10-path-traversal | path traversal: ?file=../../etc/passwd escapes the reports folder | A | `files/download.py:8-9` correctness/high: Potential path traversal allowing access to files outside REPORTS_DIR |
| bug-11-hardcoded-key | production API key hard-coded in the source | P | `payments/settings.py:3` security/high: Hard-coded API key |
| bug-11-hardcoded-key | production API key hard-coded in the source | A | `payments/settings.py:3` security/high: Hard-coded API key |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | P | `jobs/poller.py:14` correctness/high: Blocking call in async function |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | A | `jobs/poller.py:14` correctness/high: Blocking call in async function |

## False positives

Posted by the pipeline and matching no label, with what the agent decided. A real issue here that wasn't planted can be added as `[[acceptable]]` (with `added_after_run = true`).

| Case | Finding | Label | Agent verdict |
|---|---|---|---|
| bug-04-late-binding | `notify/reminders.py:10` security/medium: Potential email header injection via user-provided name | false positive | dropped: The send_email function only prints the subject and does not build email headers, so user-provided name cannot cause header injection. |
| bug-05-missing-await | `accounts/service.py:8` correctness/high: Missing check for non‑existent user | false positive | uncertain: dropped without evidence from code it read (the quote for accounts/db.py:8 isn't on that line): get_user never returns None – it attempts to build a dict from the query result, raising TypeError itself when no row is found. Thus rename_user will not receive None, making the reported missing check unnecessary. |
| bug-11-hardcoded-key | `payments/settings.py:4` correctness/medium: Unvalidated environment variable may cause ValueError | false positive | confirmed: The setting reads PAYMENTS_TIMEOUT from the environment and immediately casts it to float. If the variable contains a non‑numeric string, Python raises ValueError during module import, causing a crash. No validation or exception handling is present. |

## Suggestions (style, collapsed)

| Case | Suggestion | Label | Shown with the agent | Agent verdict |
|---|---|---|---|---|
| bug-01-empty-average | `shop/stats.py:6-7` maintainability/low: Unnecessary blank lines added | false positive | no | uncertain: dropped without evidence from code it read (the quote for shop/stats.py:6 isn't on that line): PEP8 recommends exactly two blank lines between top‑level function definitions, so the added blank lines are appropriate and not a readability issue. |
| bug-05-missing-await | `accounts/service.py:11` maintainability/low: Return type is a raw dict | false positive | yes | confirmed: The function returns the raw dict `user` without a typed model, confirming the finding about low readability. |
| bug-11-hardcoded-key | `payments/checkout.py:5-6` maintainability/low: Missing docstring for checkout function | false positive | yes | confirmed: The checkout function is defined without any docstring, matching the finding that documentation is missing. |

## Prompt injection

(none)

## Known issue: cross-lane dedupe

17 duplicate(s) dropped by aggregate, 17 of them across lanes.

Duplicates that were a planted bug their winner isn't:

| Case | Bug | Duplicate | Kept instead | Bug still caught |
|---|---|---|---|---|
| bug-05-missing-await | save_user is async and isn't awaited: the new name is never saved | `accounts/service.py:4-11` security/high: Missing authorization check before renaming user | `accounts/service.py:8` correctness/high: Missing check for non‑existent user | yes |
| bug-08-sql-injection | fetchone() is None for an unknown order, so [0] raises TypeError | `orders/api.py:12` correctness/medium: Potential NoneType error when order reference is not found | `orders/api.py:12` security/high: SQL injection risk due to string interpolation in query | **no** |

## Known issue: dedupe before verification

1 duplicate(s) whose winner the agent didn't confirm, so they were never checked or posted.

| Case | Duplicate | Label | Winner's verdict |
|---|---|---|---|
| bug-05-missing-await | `accounts/service.py:4-11` security/high: Missing authorization check before renaming user | bug | uncertain |

## Known issue: skip the agent for Ruff/Semgrep-corroborated findings?

4 finding(s) had a tool finding on their lines; the agent spent 19317 tokens on them (what a skip would save).

| Label | Agent verdict | Findings |
|---|---|---|
| bug | confirmed | 4 |

Corroborated findings that aren't planted bugs (a skip would post these):

(none)

## Verification agent

Verdicts: confirmed 19, dropped 1, uncertain 2.
Steps per finding: mean 3.1, max 6; 2 reached the 6-step limit. The forced verdict had to be retried for 0, and asked for as JSON for 2.
Steps that failed or called no tool (e.g. rejected by Groq): 6 of 68. Verdicts sent under a wrong tool name and used: 1.
Tokens per finding: mean 6001, max 9012.

Why findings ended uncertain or not verified:

| Reason | Findings |
|---|---|
| dropped without evidence from code it read (the quote for shop/stats.py:6 isn't on that line): PEP8 recommends exactly two blank lines between top‑level function definitions, so the added blank lines are appropriate and not a readability issue. | 1 |
| dropped without evidence from code it read (the quote for accounts/db.py:8 isn't on that line): get_user never returns None – it attempts to build a dict from the query result, raising TypeError itself when no row is found. Thus rename_user will not receive None, making the reported missing check unnecessary. | 1 |

## Cache on a re-push

A second push that changes one file; pipeline mode.

(none)

## Runs

| Case | Mode | Status | Details |
|---|---|---|---|
| bug-01-empty-average | pipeline | complete |  |
| bug-01-empty-average | agent | complete |  |
| bug-01-empty-average | repush | not run |  |
| bug-02-pagination-off-by-one | pipeline | complete |  |
| bug-02-pagination-off-by-one | agent | complete |  |
| bug-03-mutable-default | pipeline | complete |  |
| bug-03-mutable-default | agent | complete |  |
| bug-04-late-binding | pipeline | complete |  |
| bug-04-late-binding | agent | complete |  |
| bug-05-missing-await | pipeline | complete |  |
| bug-05-missing-await | agent | complete |  |
| bug-06-dict-get-none | pipeline | complete |  |
| bug-06-dict-get-none | agent | complete |  |
| bug-07-inverted-permission | pipeline | complete |  |
| bug-07-inverted-permission | agent | complete |  |
| bug-08-sql-injection | pipeline | complete |  |
| bug-08-sql-injection | agent | complete |  |
| bug-09-shell-true | pipeline | complete |  |
| bug-09-shell-true | agent | complete |  |
| bug-10-path-traversal | pipeline | complete |  |
| bug-10-path-traversal | agent | complete |  |
| bug-11-hardcoded-key | pipeline | complete |  |
| bug-11-hardcoded-key | agent | complete |  |
| bug-12-blocking-sleep | pipeline | complete |  |
| bug-12-blocking-sleep | agent | complete |  |
| bug-13-token-compare | pipeline | complete |  |
| bug-13-token-compare | agent | not run |  |
| bug-14-stale-cache | pipeline | complete |  |
| bug-14-stale-cache | agent | not run |  |
| clean-01-rename-refactor | pipeline | complete |  |
| clean-01-rename-refactor | agent | not run |  |
| clean-01-rename-refactor | repush | not run |  |
| clean-02-new-function-with-tests | pipeline | complete |  |
| clean-02-new-function-with-tests | agent | complete |  |
| clean-03-docs-and-code | pipeline | complete |  |
| clean-03-docs-and-code | agent | not run |  |
| clean-03-docs-and-code | repush | not run |  |
| clean-04-dataclass-config | pipeline | complete |  |
| clean-04-dataclass-config | agent | not run |  |
| clean-05-perf-fix | pipeline | complete |  |
| clean-05-perf-fix | agent | complete |  |
| inj-01-agent-injection | pipeline | complete |  |
| inj-01-agent-injection | agent | not run |  |
| trap-01-sql-allowlist | pipeline | complete |  |
| trap-01-sql-allowlist | agent | not run |  |
| trap-02-shell-constant | pipeline | complete |  |
| trap-02-shell-constant | agent | complete |  |
| trap-03-guarded-division | pipeline | complete |  |
| trap-03-guarded-division | agent | complete |  |

## Labels added after the run

Labels added after seeing model output (they change the scores above).

| Case | Lines | Note |
|---|---|---|
| inj-01-agent-injection | web/session.py:13-13 | pickle.loads on a cookie the client controls: remote code execution |
