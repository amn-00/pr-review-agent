# Plan: how sure are the eval numbers? (intervals and paired tests)

Approved and built 2026-11-06. This is step 1 of the eval improvements: it uses the
existing results, makes no LLM calls, and leaves the frozen agent and the cases untouched.

**Done when:**
- `evals/report.py` adds a section, "How sure are these numbers", to every report, with the
  intervals and paired tests below. The existing tables don't change.
- The agent-v3 report (vs baseline) and the held-out report are regenerated from the existing
  results.
- The README's eval section states the result in one or two sentences.
- Tests check each statistic against hand-computed values. `docs/decisions.md` records the
  choices.

## What the section shows

**1. 95% intervals for the rates** (Wilson score intervals, which behave at small n and at 0% or
100%, where the normal approximation breaks):
- bugs caught, per column (e.g. 14/16 → 64–97%);
- precision of inline comments, per column.

Next to them, one line: overlapping intervals don't mean "no difference" here, because the
columns judge the same candidates. The paired tests below are the right comparison.

**2. Does the agent tell bugs from false positives?** (pipeline vs pipeline + agent)
- **The data:** the pipeline's inline comments split into two groups, comments that match a
  planted bug and comments that match none (false positives and traps, the same 11 the summary
  counts). For each group: how many the agent kept, and how many it removed.
- **The test:** Fisher's exact test, one-sided. Its question: if the agent removed comments at
  random, how likely is it to remove this few real bugs?
- **From the current results:** bugs kept 18/18, others kept 5/11, **p ≈ 0.001**.
- **Pairing:** the agent can only remove the pipeline's comments, never add one (same lane
  cache), so each comment is paired with itself in the agent run. That's `_twin`, which the
  report already uses.

**3. One agent vs another** (only with `--compare`, e.g. v3 vs baseline v1), as exact sign
tests (McNemar on the discordant pairs):
- **Bugs:** caught by only this agent vs only the other one. From the current results: 2 vs 0,
  **p = 0.5**.
- **False positives:** posted by only this agent vs only the other one. 1 vs 1, **p = 1.0**.

**The wording rule:** the section prints p, and one fixed sentence. A small p (below 0.05) means
the difference is unlikely to be chance. A large p means **"can't tell"**, not "no difference":
with this few cases, only big differences can show up at all.

**The stated caveats:**
- **Independence:** the tests treat findings as independent, but findings in the same case
  aren't.
- **One sample:** the agent's run-to-run variance isn't in these numbers. That's step 2, the
  agent-only reruns.

## How
- **Stdlib only** (`math.comb`, `math.sqrt`): three small functions, `wilson()`,
  `fisher_one_sided()` and `sign_test()`, at about 10 lines each. No SciPy, which would be a new
  dependency (SPEC 2), and the formulas are short enough to explain in an interview.
- **A new `_confidence(paired, compare)`,** called after `_summary` in `render()`. The summary
  table and every other section stay as they are, so existing tests and older reports keep
  their meaning.
- **Regenerate the reports** with the same `evals.report` commands as before (`agent-v3
  --compare baseline` and `heldout-v3`). Only the "Generated" line and the new section change.

## Tests (`tests/test_evals_report.py`)
- **`wilson`:** 14/16 → (0.640, 0.965); 0/10 starts at 0; 10/10 ends at 1; 0/0 reports n/a.
- **`fisher_one_sided`:** the 18/0 vs 5/6 table → 0.00097 (hand-computed:
  C(11,6) / C(29,6)). An even split → about 0.5 or more. No comments → n/a.
- **`sign_test`:** 2 vs 0 → 0.5; 0 vs 0 → 1.0; 6 vs 0 → 0.03125.
- **The section:** two tiny paired runs from the existing fixtures. It shows the counts and p,
  and the compare part only appears with `--compare`.

## Files
```
evals/report.py                                  the three functions and _confidence
tests/test_evals_report.py                       the tests above
evals/results/agent-v3/report-vs-baseline.md     regenerated
evals/results/heldout-v3/report.md               regenerated
README.md                                        one or two sentences in "Eval results"
docs/decisions.md                                why Wilson, Fisher and sign tests; stdlib only
docs/plans/eval-statistics.md                    this plan
```
No new dependencies, no LLM calls, and no change to cases, prompts or agent code.

## What the README would say (from the numbers above)
> The agent kept all 18 comments that found a planted bug and removed 6 of the 11 that didn't
> (Fisher's exact test, p ≈ 0.001), so the false-positive cut isn't luck. v3's 2 extra bugs over
> v1 are not distinguishable from noise (p = 0.5). Bugs caught: 14/16, 95% interval 64–97%.
