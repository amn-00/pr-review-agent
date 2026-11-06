# Eval report: heldout-v3

Generated 2026-11-06 16:24 UTC from `evals/results/heldout-v3/`.

**Read this first.**
- This is a synthetic benchmark of 4 small Python cases, written for this project. The
  bugs were planted by the same people who built the reviewer, so it shows how the parts
  behave relative to each other, not how well the reviewer does on real-world PRs.
- **One sample per case and mode, with no repeat runs.** The lane cache makes every agent judge
  exactly the same candidates, but the LLM's run-to-run variance was not measured: a rerun could
  differ. **A difference of one finding between two columns is within noise**; don't read it as
  one agent being better than another. With this few cases, 2 findings isn't much more.
- Latency is on Groq's free tier, where most of it is waiting for the per-minute token limit.
- **Held-out cases.** These were written after the last agent prompt and never used to tune
  it. They're a check that the tuning generalizes, not a measurement: there are only a few, so
  a single finding moves every rate a lot.
- **An older prompt on today's code.** At least one agent here ran an older prompt version
  (`--agent-prompt`) with today's agent code: the evidence quote check and the handling of
  misnamed verdicts apply to it too. Its column compares prompts, not the agent as it was.

## Pipeline only vs pipeline + agent

**Scoring rule (applied to all runs):** precision, false positives and traps count **inline comments only**: findings in the categories bug, performance and security that passed aggregation and, with the agent, were confirmed. Style findings (readability, idiom) are collapsed suggestions: counted and listed separately, never scored. Every run is scored this way, including runs made before the rule existed, so numbers in older reports differ.

4 of 4 cases have complete results in all three.

| Metric | Pipeline only | Pipeline + agent (heldout-v1, agent v1) | Pipeline + agent (agent v3) |
|---|---|---|---|
| Bugs caught | 2/2 (100%) | 2/2 (100%) | 2/2 (100%) |
| Bugs lost to an uncertain verdict (of which, by the evidence check) | — | 0 (0) | 0 (0) |
| Traps posted | 0/1 | 0/1 | 0/1 |
| Clean PRs with a false positive | 0/1 | 0/1 | 0/1 |
| False positives | 0 (0.00 per PR) | 0 (0.00 per PR) | 0 (0.00 per PR) |
| Inline comments | 2 | 2 | 2 |
| Precision (inline comments) | 100% | 100% | 100% |
| Suggestions (collapsed, not scored) | 1 | 1 | 1 |
| Cost per PR (list prices) | $0.0008 (max $0.0013) | $0.0014 (max $0.0018) | $0.0015 (max $0.0019) |
| Latency per PR | 6 s (max 7 s) | 17 s (max 20 s) | 16 s (max 18 s) |
| Rate-limit waits per PR (summed over parallel calls) | 0 s | 0 s | 0 s |

**Over-correction check, against `heldout-v1`:** bugs caught +0, traps posted +0, clean PRs with a false positive +0, false positives +0. This agent wins only if bugs caught don't go down, none of the other three go up, and at least one of the four gets better: **doesn't pass**. With one sample per finding, a change of 1–2 can be noise.

Pipeline + agent cost and latency = the pipeline run's plus the agent's (the agent run got every lane call from the cache). Latency leaves out runs that took more than one attempt. False positives are inline comments that match no planted bug and no `acceptable` label; precision = inline comments matching a planted bug / all inline comments that aren't `acceptable`. A bug lost to an uncertain verdict was reported by a finding that passed aggregation but that the agent ended `uncertain`; the number in brackets is how many of those the evidence check made uncertain (a quote not on its line, or a line the agent wasn't shown).

## How sure are these numbers

| 95% interval | Pipeline only | Pipeline + agent (heldout-v1, agent v1) | Pipeline + agent (agent v3) |
|---|---|---|---|
| Bugs caught | 2/2: 34–100% | 2/2: 34–100% | 2/2: 34–100% |
| Precision (inline comments) | 2/2: 34–100% | 2/2: 34–100% | 2/2: 34–100% |

The columns judge the same candidates, so overlapping intervals don't mean "no difference". The paired tests below are the right comparison.

**Does the agent remove false positives rather than bugs?**

The agent removed none of the pipeline's inline comments.

**This agent against `heldout-v1`** (exact sign test on what only one of the two got):

- Bugs caught by only this agent: 0; by only `heldout-v1`: 0. **p = 1**
- False positives posted by only this agent: 0; by only `heldout-v1`: 0. **p = 1**

**How to read p:** below 0.05, the difference is unlikely to be chance. A large p means **can't tell**, not "no difference": with this few cases, only big differences can show at all. The tests treat findings as independent, though findings in the same case aren't, and the agent's run-to-run variance isn't in these numbers (one sample per case).

**Traps that test nothing.** No lane reported anything on these, so there was no false positive for the agent to drop. They count in "Traps posted" as 0/1 in every column:

- `held-04-retry-jitter`: random used for backoff jitter, which needs no cryptographic randomness

## Changed verdicts

(none)

## Per case

| Case | Kind | Caught (P / A) | FP (P / A) | Cost (P / A) | Wall (P run / A run) | Notes |
|---|---|---|---|---|---|---|
| held-01-bill-split | bug | 1 / 1 of 1 | 0 / 0 | $0.0007 / $0.0019 | 5 / 12 s |  |
| held-02-reset-token | bug | 1 / 1 of 1 | 0 / 0 | $0.0007 / $0.0018 | 5 / 12 s |  |
| held-03-batch-upload | clean | 0 / 0 of 0 | 0 / 0 | $0.0013 / $0.0017 | 7 / 12 s |  |
| held-04-retry-jitter | trap | 0 / 0 of 0 | 0 / 0 | $0.0006 / $0.0006 | 7 / 5 s |  |

## Where each bug was lost

| Case | Bug | A lane found it | Survived aggregate | Posted (P) | Confirmed (A) |
|---|---|---|---|---|---|
| held-01-bill-split | integer division drops the remainder: the shares don't add up to the total | yes | yes | yes | yes |
| held-02-reset-token | reset token from the random module, which is predictable: use secrets | yes | yes | yes | yes |

## Matches

Every catch, so the automatic matching can be checked by eye.

| Case | Bug | Mode | Finding |
|---|---|---|---|
| held-01-bill-split | integer division drops the remainder: the shares don't add up to the total | P | `billing/split.py:9-10` correctness/high: Share distribution may not sum to total amount |
| held-01-bill-split | integer division drops the remainder: the shares don't add up to the total | A | `billing/split.py:9-10` correctness/high: Share distribution may not sum to total amount |
| held-02-reset-token | reset token from the random module, which is predictable: use secrets | P | `accounts/reset.py:8` security/high: Insecure random number generator for password reset token |
| held-02-reset-token | reset token from the random module, which is predictable: use secrets | A | `accounts/reset.py:8` security/high: Insecure random number generator for password reset token |

## False positives

Posted by the pipeline and matching no label, with what the agent decided. A real issue here that wasn't planted can be added as `[[acceptable]]` (with `added_after_run = true`).

(none)

## Suggestions (style, collapsed)

| Case | Suggestion | Label | Shown with the agent | Agent verdict |
|---|---|---|---|---|
| held-03-batch-upload | `batching/upload.py:6-9` maintainability/medium: Function returns total records regardless of upload success | false positive | yes | confirmed: The function uploads batches but never inspects the result of client.post and always returns the total number of input records, so callers cannot know how many were actually uploaded. |

## Prompt injection

(none)

## Known issue: cross-lane dedupe

3 duplicate(s) dropped by aggregate, 2 of them across lanes.

Duplicates that were a planted bug their winner isn't:

(none)

## Known issue: dedupe before verification

0 duplicate(s) whose winner the agent didn't confirm, so they were never checked or posted.

(none)

## Known issue: skip the agent for Ruff/Semgrep-corroborated findings?

0 finding(s) had a tool finding on their lines; the agent spent 0 tokens on them (what a skip would save).

(none)

Corroborated findings that aren't planted bugs (a skip would post these):

(none)

## Verification agent

Verdicts: confirmed 3.
Steps per finding: mean 2.3, max 3; 0 reached the 6-step limit. The forced verdict had to be retried for 0, and asked for as JSON for 0.
Steps that failed or called no tool (e.g. rejected by Groq): 0 of 7. Verdicts sent under a wrong tool name and used: 0.
Tokens per finding: mean 5030, max 6526.

Why findings ended uncertain or not verified:

(none)

## Cache on a re-push

A second push that changes one file; pipeline mode.

(none)

## Runs

| Case | Mode | Status | Details |
|---|---|---|---|
| held-01-bill-split | pipeline | complete |  |
| held-01-bill-split | agent | complete |  |
| held-02-reset-token | pipeline | complete |  |
| held-02-reset-token | agent | complete |  |
| held-03-batch-upload | pipeline | complete |  |
| held-03-batch-upload | agent | complete |  |
| held-04-retry-jitter | pipeline | complete |  |
| held-04-retry-jitter | agent | complete |  |

## Labels added after the run

Labels added after seeing model output (they change the scores above).

(none)
