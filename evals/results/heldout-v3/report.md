# Eval report: heldout-v3

Generated 2026-11-04 10:42 UTC from `evals/results/heldout-v3/`.

**Read this first.**
- This is a synthetic benchmark of 4 small Python cases, written for this project. The
  bugs were planted by the same people who built the reviewer, so it shows how the parts
  behave relative to each other, not how well the reviewer does on real-world PRs.
- One sample per case and mode. The lane cache makes both modes judge exactly the same
  candidates, but the LLM's run-to-run variance is not measured: a rerun could differ.
- With this few cases, a difference of 1–2 findings is not meaningful.
- Latency is on Groq's free tier, where most of it is waiting for the per-minute token limit.
- **Held-out cases.** These were written after the last agent prompt and never used to tune
  it. They're a check that the tuning generalizes, not a measurement: there are only a few, so
  a single finding moves every rate a lot.
- **An older prompt on today's code.** At least one agent here ran an older prompt version
  (`--agent-prompt`) with today's agent code: the evidence quote check and the handling of
  misnamed verdicts apply to it too. Its column compares prompts, not the agent as it was.

## Pipeline only vs pipeline + agent

**Scoring rule (applied to all runs):** precision, false positives and traps count **inline comments only**: findings in the categories bug, performance and security that passed aggregation and, with the agent, were confirmed. Style findings (readability, idiom) are collapsed suggestions: counted and listed separately, never scored. Every run is scored this way, including runs made before the rule existed, so numbers in older reports differ.

1 of 4 cases have complete results in all three.

| Metric | Pipeline only | Pipeline + agent (heldout-v1, agent v1) | Pipeline + agent (agent v3) |
|---|---|---|---|
| Bugs caught | 0/0 (n/a) | 0/0 (n/a) | 0/0 (n/a) |
| Bugs lost to an uncertain verdict (of which, by the evidence check) | — | 0 (0) | 0 (0) |
| Traps posted | 0/1 | 0/1 | 0/1 |
| Clean PRs with a false positive | 0/0 | 0/0 | 0/0 |
| False positives | 0 (0.00 per PR) | 0 (0.00 per PR) | 0 (0.00 per PR) |
| Inline comments | 0 | 0 | 0 |
| Precision (inline comments) | n/a | n/a | n/a |
| Suggestions (collapsed, not scored) | 0 | 0 | 0 |
| Cost per PR (list prices) | $0.0006 (max $0.0006) | $0.0006 (max $0.0006) | $0.0006 (max $0.0006) |
| Latency per PR | 7 s (max 7 s) | 11 s (max 11 s) | 12 s (max 12 s) |
| Rate-limit waits per PR (summed over parallel calls) | 0 s | 0 s | 0 s |

**Over-correction check, against `heldout-v1`:** bugs caught +0, traps posted +0, clean PRs with a false positive +0, false positives +0. This agent wins only if bugs caught don't go down, none of the other three go up, and at least one of the four gets better: **doesn't pass**. With one sample per finding, a change of 1–2 can be noise.

Pipeline + agent cost and latency = the pipeline run's plus the agent's (the agent run got every lane call from the cache). Latency leaves out runs that took more than one attempt. False positives are inline comments that match no planted bug and no `acceptable` label; precision = inline comments matching a planted bug / all inline comments that aren't `acceptable`. A bug lost to an uncertain verdict was reported by a finding that passed aggregation but that the agent ended `uncertain`; the number in brackets is how many of those the evidence check made uncertain (a quote not on its line, or a line the agent wasn't shown).

**Traps that test nothing.** No lane reported anything on these, so there was no false positive for the agent to drop. They count in "Traps posted" as 0/1 in every column:

- `held-04-retry-jitter`: random used for backoff jitter, which needs no cryptographic randomness

## Changed verdicts

(none)

## Per case

| Case | Kind | Caught (P / A) | FP (P / A) | Cost (P / A) | Wall (P run / A run) | Notes |
|---|---|---|---|---|---|---|
| held-04-retry-jitter | trap | 0 / 0 of 0 | 0 / 0 | $0.0006 / $0.0006 | 7 / 5 s |  |

## Where each bug was lost

(none)

## Matches

Every catch, so the automatic matching can be checked by eye.

(none)

## False positives

Posted by the pipeline and matching no label, with what the agent decided. A real issue here that wasn't planted can be added as `[[acceptable]]` (with `added_after_run = true`).

(none)

## Suggestions (style, collapsed)

(none)

## Prompt injection

(none)

## Known issue: cross-lane dedupe

0 duplicate(s) dropped by aggregate, 0 of them across lanes.

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

No agent steps yet.

## Cache on a re-push

A second push that changes one file; pipeline mode.

(none)

## Runs

| Case | Mode | Status | Details |
|---|---|---|---|
| held-01-bill-split | pipeline | complete |  |
| held-01-bill-split | agent | not run |  |
| held-02-reset-token | pipeline | complete |  |
| held-02-reset-token | agent | not run |  |
| held-03-batch-upload | pipeline | complete |  |
| held-03-batch-upload | agent | not run |  |
| held-04-retry-jitter | pipeline | complete |  |
| held-04-retry-jitter | agent | complete |  |

## Labels added after the run

Labels added after seeing model output (they change the scores above).

(none)
