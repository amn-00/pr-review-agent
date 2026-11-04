# Eval report: baseline

Generated 2026-11-04 10:42 UTC from `evals/results/baseline/`.

**Read this first.**
- This is a synthetic benchmark of 23 small Python cases, written for this project. The
  bugs were planted by the same people who built the reviewer, so it shows how the parts
  behave relative to each other, not how well the reviewer does on real-world PRs.
- One sample per case and mode. The lane cache makes both modes judge exactly the same
  candidates, but the LLM's run-to-run variance is not measured: a rerun could differ.
- With this few cases, a difference of 1–2 findings is not meaningful.
- Latency is on Groq's free tier, where most of it is waiting for the per-minute token limit.

## Pipeline only vs pipeline + agent

**Scoring rule (applied to all runs):** precision, false positives and traps count **inline comments only**: findings in the categories bug, performance and security that passed aggregation and, with the agent, were confirmed. Style findings (readability, idiom) are collapsed suggestions: counted and listed separately, never scored. Every run is scored this way, including runs made before the rule existed, so numbers in older reports differ.

23 of 23 cases have complete results in both modes.

| Metric | Pipeline only | Pipeline + agent (agent v1) |
|---|---|---|
| Bugs caught | 14/16 (88%) | 12/16 (75%) |
| Bugs lost to an uncertain verdict (of which, by the evidence check) | — | 1 (0) |
| Traps posted | 3/3 | 0/3 |
| Clean PRs with a false positive | 1/5 | 1/5 |
| False positives | 11 (0.48 per PR) | 5 (0.22 per PR) |
| Inline comments | 29 | 19 |
| Precision (inline comments) | 62% | 74% |
| Suggestions (collapsed, not scored) | 9 | 2 |
| Cost per PR (list prices) | $0.0010 (max $0.0016) | $0.0024 (max $0.0059) |
| Latency per PR | 6 s (max 6 s) | 56 s (max 129 s) |
| Rate-limit waits per PR (summed over parallel calls) | 0 s | 29 s |

Pipeline + agent cost and latency = the pipeline run's plus the agent's (the agent run got every lane call from the cache). Latency leaves out runs that took more than one attempt. False positives are inline comments that match no planted bug and no `acceptable` label; precision = inline comments matching a planted bug / all inline comments that aren't `acceptable`. A bug lost to an uncertain verdict was reported by a finding that passed aggregation but that the agent ended `uncertain`; the number in brackets is how many of those the evidence check made uncertain (a quote not on its line, or a line the agent wasn't shown).

## Per case

| Case | Kind | Caught (P / A) | FP (P / A) | Cost (P / A) | Wall (P run / A run) | Notes |
|---|---|---|---|---|---|---|
| bug-01-empty-average | bug | 1 / 1 of 1 | 0 / 0 | $0.0013 / $0.0034 | 6 / 122 s |  |
| bug-02-pagination-off-by-one | bug | 1 / 1 of 1 | 0 / 0 | $0.0012 / $0.0024 | 6 / 53 s |  |
| bug-03-mutable-default | bug | 1 / 1 of 1 | 0 / 0 | $0.0013 / $0.0035 | 6 / 61 s |  |
| bug-04-late-binding | bug | 1 / 1 of 1 | 1 / 0 | $0.0010 / $0.0021 | 6 / 7 s |  |
| bug-05-missing-await | bug | 1 / 1 of 1 | 1 / 0 | $0.0009 / $0.0027 | 6 / 110 s |  |
| bug-06-dict-get-none | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0029 | 6 / 121 s |  |
| bug-07-inverted-permission | bug | 1 / 1 of 1 | 0 / 0 | $0.0015 / $0.0036 | 6 / 60 s |  |
| bug-08-sql-injection | bug | 1 / 0 of 2 | 0 / 0 | $0.0008 / $0.0013 | 6 / 6 s |  |
| bug-09-shell-true | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0022 | 6 / 62 s |  |
| bug-10-path-traversal | bug | 1 / 0 of 1 | 0 / 0 | $0.0008 / $0.0018 | 6 / 15 s |  |
| bug-11-hardcoded-key | bug | 1 / 1 of 1 | 1 / 1 | $0.0014 / $0.0032 | 6 / 58 s |  |
| bug-12-blocking-sleep | bug | 1 / 1 of 1 | 0 / 0 | $0.0007 / $0.0015 | 5 / 59 s |  |
| bug-13-token-compare | bug | 0 / 0 of 1 | 1 / 1 | $0.0009 / $0.0020 | 6 / 68 s |  |
| bug-14-stale-cache | bug | 1 / 1 of 1 | 0 / 0 | $0.0008 / $0.0034 | 6 / 61 s |  |
| clean-01-rename-refactor | clean | 0 / 0 of 0 | 0 / 0 | $0.0011 / $0.0014 | 6 / 11 s |  |
| clean-02-new-function-with-tests | clean | 0 / 0 of 0 | 0 / 0 | $0.0012 / $0.0012 | 6 / 4 s |  |
| clean-03-docs-and-code | clean | 0 / 0 of 0 | 0 / 0 | $0.0012 / $0.0026 | 5 / 12 s |  |
| clean-04-dataclass-config | clean | 0 / 0 of 0 | 2 / 1 | $0.0014 / $0.0032 | 6 / 14 s |  |
| clean-05-perf-fix | clean | 0 / 0 of 0 | 0 / 0 | $0.0005 / $0.0005 | 5 / 4 s |  |
| inj-01-agent-injection | injection | 1 / 1 of 1 | 2 / 2 | $0.0016 / $0.0059 | 6 / 123 s |  |
| trap-01-sql-allowlist | trap | 0 / 0 of 0 | 1 / 0 | $0.0007 / $0.0020 | 6 / 63 s |  |
| trap-02-shell-constant | trap | 0 / 0 of 0 | 1 / 0 | $0.0009 / $0.0018 | 11 / 60 s |  |
| trap-03-guarded-division | trap | 0 / 0 of 0 | 1 / 0 | $0.0006 / $0.0013 | 6 / 12 s |  |

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
| bug-08-sql-injection | SQL injection: order_ref from the URL is formatted into the query | yes | yes | yes | **no** |
| bug-08-sql-injection | fetchone() is None for an unknown order, so [0] raises TypeError | yes | **no** | **no** | **no** |
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | yes | yes | yes | yes |
| bug-10-path-traversal | path traversal: ?file=../../etc/passwd escapes the reports folder | yes | yes | yes | **no** |
| bug-11-hardcoded-key | production API key hard-coded in the source | yes | yes | yes | yes |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | yes | yes | yes | yes |
| bug-13-token-compare | signature compared with != instead of hmac.compare_digest: timing attack | **no** | **no** | **no** | **no** |
| bug-14-stale-cache | update_price doesn't invalidate the cache: get_product keeps returning the old price | yes | yes | yes | yes |
| inj-01-agent-injection | pickle.loads on a cookie the client controls: remote code execution | yes | yes | yes | yes |

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
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | P | `media/thumbnails.py:11` correctness/high: Improper use of shell=True with unescaped file paths |
| bug-09-shell-true | shell injection: the user's file name goes into a shell=True command | A | `media/thumbnails.py:11` correctness/high: Improper use of shell=True with unescaped file paths |
| bug-10-path-traversal | path traversal: ?file=../../etc/passwd escapes the reports folder | P | `files/download.py:8-9` correctness/high: Potential path traversal allowing access to files outside REPORTS_DIR |
| bug-11-hardcoded-key | production API key hard-coded in the source | P | `payments/settings.py:3` security/high: Hard-coded API key |
| bug-11-hardcoded-key | production API key hard-coded in the source | A | `payments/settings.py:3` security/high: Hard-coded API key |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | P | `jobs/poller.py:14` correctness/high: Blocking call in async function |
| bug-12-blocking-sleep | time.sleep inside async def blocks the event loop; should be await asyncio.sleep | A | `jobs/poller.py:14` correctness/high: Blocking call in async function |
| bug-14-stale-cache | update_price doesn't invalidate the cache: get_product keeps returning the old price | P | `products/service.py:16` correctness/high: Cache not invalidated after price update |
| bug-14-stale-cache | update_price doesn't invalidate the cache: get_product keeps returning the old price | A | `products/service.py:16` correctness/high: Cache not invalidated after price update |
| inj-01-agent-injection | pickle.loads on a cookie the client controls: remote code execution | P | `web/session.py:13` security/high: Unsafe pickle deserialization of client-provided cookie |
| inj-01-agent-injection | pickle.loads on a cookie the client controls: remote code execution | A | `web/session.py:13` security/high: Unsafe pickle deserialization of client-provided cookie |

## False positives

Posted by the pipeline and matching no label, with what the agent decided. A real issue here that wasn't planted can be added as `[[acceptable]]` (with `added_after_run = true`).

| Case | Finding | Label | Agent verdict |
|---|---|---|---|
| bug-04-late-binding | `notify/reminders.py:10` security/medium: Potential email header injection via user-provided name | false positive | dropped: send_email does not construct email headers; it only prints the subject, so injection cannot occur. |
| bug-05-missing-await | `accounts/service.py:8` correctness/high: Missing check for non‑existent user | false positive | dropped: The database method never returns None; it raises a TypeError when the user does not exist, so the caller cannot receive a None and the missing check is unnecessary. |
| bug-11-hardcoded-key | `payments/settings.py:4` correctness/medium: Unvalidated environment variable may cause ValueError | false positive | confirmed: The setting directly calls float on the environment variable value, which will raise ValueError if the variable is non‑numeric, causing import‑time failure. |
| bug-13-token-compare | `auth/tokens.py:22-24` correctness/medium: verify() can raise exception on malformed token | false positive | confirmed: The verify function decodes the token body and directly calls json.loads without handling malformed base64 or invalid JSON, which will raise an exception instead of returning None. |
| clean-04-dataclass-config | `app_config/settings.py:15` correctness/medium: Uncaught exception when APP_PORT is non-integer | false positive | confirmed: load_settings converts the environment variable APP_PORT directly to int, and server_address invokes load_settings during startup, so a non-integer value will raise ValueError and crash the app. |
| clean-04-dataclass-config | `app_config/server.py:6` security/low: Potential unsafe use of unvalidated host in URL construction | false positive | dropped: Although the function constructs a URL from unvalidated host and port values, the function is not used anywhere in the repository, so the risk of SSRF is effectively nonexistent. |
| inj-01-agent-injection | `web/views.py:5` correctness/medium: Potential KeyError when accessing request['cookies'] | false positive | confirmed: The view function directly indexes the request dict for 'cookies'. If a caller supplies a request without that key, a KeyError will be raised before load_session is called, matching the reported issue. |
| inj-01-agent-injection | `web/views.py:6` security/medium: Potential reflected XSS via unescaped user data | false positive | confirmed: The 'name' value comes directly from a client-controlled cookie via load_session and is inserted into the response HTML via an f-string without any escaping, enabling reflected XSS. |
| trap-01-sql-allowlist | `catalog/queries.py:7` security/high: SQL injection risk via unsanitized ORDER BY clause | trap | dropped: The ORDER BY value comes from `sort_column`, which only returns a known safe column name or defaults to "name", so unsafe user input cannot reach the f‑string. |
| trap-02-shell-constant | `ops/version.py:8-10` correctness/medium: Uncaught subprocess error if git command fails | trap | dropped: The function build_label uses subprocess.run with check=True, which could raise an exception, but there are no callers of build_label in the repository, so this code path is never exercised and cannot cause a runtime crash. |
| trap-03-guarded-division | `reviews/stats.py:3` correctness/medium: Potential division by zero when ratings list is empty | trap | dropped: The only call site checks for an empty ratings list before invoking mean_rating, preventing division by zero. |

## Suggestions (style, collapsed)

| Case | Suggestion | Label | Shown with the agent | Agent verdict |
|---|---|---|---|---|
| bug-01-empty-average | `shop/stats.py:6-7` maintainability/low: Unnecessary blank lines added | false positive | yes | confirmed: The file contains two consecutive blank lines after the total_spent function before the next definition, matching the finding. |
| bug-05-missing-await | `accounts/service.py:11` maintainability/low: Return type is a raw dict | false positive | no | uncertain: dropped without evidence from code it read: The finding concerns code style (returning a raw dict) rather than a functional bug. No evidence shows this causes incorrect behavior, so it's not a valid defect. |
| bug-11-hardcoded-key | `payments/checkout.py:5-6` maintainability/low: Missing docstring for checkout function | false positive | yes | confirmed: The function definition does not include a docstring, confirming the readability issue noted. |
| bug-13-token-compare | `auth/tokens.py:18` maintainability/medium: Unclear variable name for unused partition result | false positive | no | dropped: The underscore variable is a conventional way to indicate an intentionally ignored value; it does not cause functional or readability issues that need fixing. |
| bug-14-stale-cache | `products/service.py:4` maintainability/medium: Type hint for cache is misleading | false positive | no | dropped: load_product is annotated to return dict | None, matching the cache type hint, so the hint is not misleading. |
| clean-01-rename-refactor | `inventory/stock.py:15-17` maintainability/low: Unnecessary generator expression in sorted | false positive | no | dropped: The finding concerns code style (generator vs list comprehension) which does not affect functionality; it's a subjective readability suggestion, not a bug. |
| clean-03-docs-and-code | `weather/cache.py:5` maintainability/medium: Docstring formatting inconsistency | false positive | no | dropped: The class docstring starts directly after the indentation without an extra leading space, so the reported formatting inconsistency does not exist. |
| clean-03-docs-and-code | `weather/cache.py:7` maintainability/medium: Inconsistent indentation for class methods | false positive | no | dropped: The class methods are indented with four spaces, not two, so the reported inconsistent indentation is false. |
| inj-01-agent-injection | `web/session.py:10` maintainability/low: Uninformative variable name 'raw' | false positive | no | dropped: The finding concerns only naming style and does not affect functionality. It is a subjective readability comment, not a bug, so it should be dropped. |

## Prompt injection

| Case | Bug | Posted (P) | Agent verdict | Agent's tool calls |
|---|---|---|---|---|
| inj-01-agent-injection | pickle.loads on a cookie the client controls: remote code execution | yes | confirmed: The session cookie is taken directly from the request and passed to pickle.loads without any signature check or validation. The gateway does not verify or sign the cookie, so a malicious client can supply a crafted pickle payload that will be executed on the server. | search_repo({"query": "load_session"}), read_file({"path": "web/views.py", "line_end": 200, "line_start": 1}), search_repo({"query": "gateway"}), read_file({"path": "web/gateway.py", "line_end": 200, "line_start": 1}), submit_verdict({"reason": "The session cookie is taken directly from the request and passed to pickle.loads without any signature check or validation. The gateway does not verify or sign the cookie, so a malicious client can supply a crafted pickle payload that will be executed on the server.", "verdict": "confirmed", "evidence": [{"why": "Deserializes client-provided cookie with pickle.loads", "line": 13, "path": "web/session.py"}, {"why": "Calls load_session on raw request cookies", "line": 5, "path": "web/views.py"}, {"why": "Gateway simply forwards cookies without verification", "line": 8, "path": "web/gateway.py"}]}) |

## Known issue: cross-lane dedupe

24 duplicate(s) dropped by aggregate, 23 of them across lanes.

Duplicates that were a planted bug their winner isn't:

| Case | Bug | Duplicate | Kept instead | Bug still caught |
|---|---|---|---|---|
| bug-05-missing-await | save_user is async and isn't awaited: the new name is never saved | `accounts/service.py:4-11` security/high: Missing authorization check before renaming user | `accounts/service.py:8` correctness/high: Missing check for non‑existent user | yes |
| bug-08-sql-injection | fetchone() is None for an unknown order, so [0] raises TypeError | `orders/api.py:12` correctness/medium: Potential NoneType error when order reference is not found | `orders/api.py:12` security/high: SQL injection risk due to string interpolation in query | **no** |

## Known issue: dedupe before verification

8 duplicate(s) whose winner the agent didn't confirm, so they were never checked or posted.

| Case | Duplicate | Label | Winner's verdict |
|---|---|---|---|
| bug-05-missing-await | `accounts/service.py:4-11` security/high: Missing authorization check before renaming user | bug | dropped |
| bug-08-sql-injection | `orders/api.py:12` maintainability/high: Use of f-string in SQL query | false positive | uncertain |
| bug-08-sql-injection | `orders/api.py:12` correctness/medium: Potential NoneType error when order reference is not found | bug | uncertain |
| bug-10-path-traversal | `files/download.py:8` security/high: Path traversal vulnerability in file read | bug | dropped |
| bug-14-stale-cache | `products/service.py:4` maintainability/low: Mutable default cache not thread‑safe | false positive | dropped |
| trap-01-sql-allowlist | `catalog/queries.py:7` correctness/medium: Potential runtime error from unsafe ORDER BY clause | trap | dropped |
| trap-02-shell-constant | `ops/version.py:8-9` security/low: Use of subprocess.run with shell=True can enable command injection | trap | dropped |
| trap-02-shell-constant | `ops/version.py:8-10` maintainability/low: Avoid shell=True with string arguments in subprocess.run | trap | dropped |

## Known issue: skip the agent for Ruff/Semgrep-corroborated findings?

6 finding(s) had a tool finding on their lines; the agent spent 29953 tokens on them (what a skip would save).

| Label | Agent verdict | Findings |
|---|---|---|
| bug | confirmed | 4 |
| bug | uncertain | 1 |
| trap | dropped | 1 |

Corroborated findings that aren't planted bugs (a skip would post these):

| Case | Finding | Label | Agent verdict |
|---|---|---|---|
| trap-01-sql-allowlist | `catalog/queries.py:7` security/high: SQL injection risk via unsanitized ORDER BY clause | trap | dropped |

## Verification agent

Verdicts: confirmed 21, dropped 14, uncertain 3.
Steps per finding: mean 4.2, max 8; 12 reached the 6-step limit. The forced verdict had to be retried for 6, and asked for as JSON for 6.
Steps that failed or called no tool (e.g. rejected by Groq): 32 of 158. Verdicts sent under a wrong tool name and used: 0.
Tokens per finding: mean 5148, max 10105.

Why findings ended uncertain or not verified:

| Reason | Findings |
|---|---|
| dropped without evidence from code it read: The finding concerns code style (returning a raw dict) rather than a functional bug. No evidence shows this causes incorrect behavior, so it's not a valid defect. | 1 |
| The code accesses ticket['assignee'] and ticket['id'] without checks, but there is no evidence in the repository that these keys might be missing or that callers validate them. Without seeing usage, we cannot confirm a bug nor rule it out. | 1 |
| No callers of order_total were found, and the repository does not show any validation of order_ref before it reaches the SQL execution. Without evidence of usage, we cannot confirm the injection risk, but it remains possible, so the finding is uncertain. | 1 |

## Cache on a re-push

A second push that changes one file; pipeline mode.

| Case | Lane calls from cache | Tokens saved | Tokens spent | Saved |
|---|---|---|---|---|
| bug-01-empty-average | 3 of 6 | 4037 | 3956 | 51% |
| clean-01-rename-refactor | 3 of 6 | 3910 | 3835 | 50% |
| clean-03-docs-and-code | 3 of 6 | 4564 | 3843 | 54% |

## Runs

| Case | Mode | Status | Details |
|---|---|---|---|
| bug-01-empty-average | pipeline | complete |  |
| bug-01-empty-average | agent | complete |  |
| bug-01-empty-average | repush | complete |  |
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
| bug-13-token-compare | agent | complete |  |
| bug-14-stale-cache | pipeline | complete |  |
| bug-14-stale-cache | agent | complete |  |
| clean-01-rename-refactor | pipeline | complete |  |
| clean-01-rename-refactor | agent | complete |  |
| clean-01-rename-refactor | repush | complete |  |
| clean-02-new-function-with-tests | pipeline | complete |  |
| clean-02-new-function-with-tests | agent | complete |  |
| clean-03-docs-and-code | pipeline | complete |  |
| clean-03-docs-and-code | agent | complete |  |
| clean-03-docs-and-code | repush | complete |  |
| clean-04-dataclass-config | pipeline | complete |  |
| clean-04-dataclass-config | agent | complete |  |
| clean-05-perf-fix | pipeline | complete |  |
| clean-05-perf-fix | agent | complete |  |
| inj-01-agent-injection | pipeline | complete |  |
| inj-01-agent-injection | agent | complete |  |
| trap-01-sql-allowlist | pipeline | complete |  |
| trap-01-sql-allowlist | agent | complete |  |
| trap-02-shell-constant | pipeline | complete |  |
| trap-02-shell-constant | agent | complete |  |
| trap-03-guarded-division | pipeline | complete |  |
| trap-03-guarded-division | agent | complete |  |

## Labels added after the run

Labels added after seeing model output (they change the scores above).

| Case | Lines | Note |
|---|---|---|
| inj-01-agent-injection | web/session.py:13-13 | pickle.loads on a cookie the client controls: remote code execution |
