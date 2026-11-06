# Plan: how much does the agent vary between runs?

Approved 2026-11-06: the held-out cases are included, and v1 isn't rerun. This is row 2 of
the eval improvements. Row 1, outside labels, was dropped.

**The question:** each metric so far comes from one run, so we can't say whether v3's "2 more
bugs than v1" is bigger than the variation of v3 against itself. The agent samples at Groq's
default temperature (we send none), so two runs on the same candidates can differ.

**Done when:**
- Agent v3 has run twice more on the 23 cases and the 4 held-out cases, on exactly the same
  candidates (lanes from the cache), every result complete.
- A repeats report shows each metric's range over the 3 runs, and how stable each verdict is.
- The README says how much the numbers move between runs.
- No change to the agent, its prompts or the cases.

## 1. A repeats report [evals/report.py]
`python -m evals.report --repeats agent-v3 agent-v3-r2 agent-v3-r3` writes
`evals/results/agent-v3/repeats.md`. The held-out runs get the same, with `--cases heldout`.

**The sections:**
- **Each run, side by side.** One column per run, plus "range":
  - bugs caught;
  - traps posted;
  - clean PRs with a false positive;
  - false positives;
  - precision;
  - Fisher's p for "removes false positives rather than bugs".

  It's built from the existing `totals()` and `fisher_one_sided()`.
- **Verdict stability.** Each candidate that passed aggregation, matched across runs with
  `_twin()`: how many got the same verdict in all 3 runs, and a list of the ones that changed,
  with their labels (bug, false positive or trap).
- **Each planted bug:** caught in 3/3, 2/3, 1/3 or 0/3 runs.
- **One sentence on v1.** Whether v1's 12/16 lies inside v3's own range. If it does, the
  v1-to-v3 difference is within v3's run-to-run noise.

**Tests:** two or three tiny hand-built runs, checking the ranges, the "same verdict in all
runs" count, the list of changed verdicts, and the per-bug counts.

## 2. The runs [no code, about 3 eval days]
Each run copies v3's pipeline results, so only the agent runs, and every lane call comes from the
cache:
```
uv run python -m evals.run --name agent-v3-r2 --pipeline-from agent-v3 --mode agent --agent-prompt v3 --agent-budget 180000
uv run python -m evals.run --name agent-v3-r3 --pipeline-from agent-v3 --mode agent --agent-prompt v3 --agent-budget 180000
uv run python -m evals.run --name heldout-v3-r2 --pipeline-from heldout-v3 --cases heldout --mode agent --agent-prompt v3 --agent-budget 180000
uv run python -m evals.run --name heldout-v3-r3 --pipeline-from heldout-v3 --cases heldout --mode agent --agent-prompt v3 --agent-budget 180000
```
- **First, check the lane cache with a pipeline-mode dry run** under a throwaway name:
  `--name cache-check --mode pipeline --dry-run`. It lists the lane tokens not in the cache.
  An agent-mode dry run only estimates the agent's own tokens, so it can't show a cache miss
  (found while building). Every case must show `~0`. If not, the candidates would differ, so
  stop and look. Checked on 2026-11-06: all 27 cases show `~0` on both models, and the check
  creates no files.
- **Quota:** one rerun is about 213K agent tokens, against at most 180K a day for the agent, so
  2 reruns plus the held-out (30K) take about 3 sessions. The runner's pre-flight check holds
  back cases that don't fit, and the next session resumes them.
- **Needs:** Docker's Postgres (the `pr_review_eval` database with its lane cache) and Semgrep
  on PATH, as in Phase 5.

**Two things that differ from the v3 run, both stated in the report:**
- **The Groq 429 handling (Phase 6c fix 3) changed after v3 ran.** It affects latency and how
  rate-limited calls are retried, not the verdicts. Latency isn't compared across these runs.
- **The deployed bot uses the same Groq account.** On eval days the evals take up to 180K of the
  daily 200K, so a real PR reviewed that day may hit Groq's daily limit and be deferred to
  midnight UTC. Don't open demo PRs on eval days.

## 3. Write-up
- **README, "Eval results":** one or two sentences, e.g. "Across 3 runs, v3 caught a–b of 16 bugs
  and posted c–d false positives; v1's 12/16 is (inside or outside) that range."
- **`docs/decisions.md`:** the method and the result.
- **`docs/engineering-log.md`:** an entry only if something breaks on the way.

## Files
```
evals/report.py                      --repeats and its section
tests/test_evals_report.py           its tests
evals/results/agent-v3-r2/, -r3/     the new runs (results and reports)
evals/results/heldout-v3-r2/, -r3/
evals/results/agent-v3/repeats.md    the repeats report
evals/results/heldout-v3/repeats.md
README.md, docs/decisions.md         the write-up
```

## Decisions for you
1. **Include the held-out cases?** I'd say yes. It costs only 30K tokens for both reruns, and
   it shows whether "nothing to test" holds across runs too.
2. **Rerun v1 as well?** I'd say not now. That would be about 2 more eval days. Rerunning v3
   already tells us whether v1's result falls inside v3's own noise. A full v1-against-v3
   comparison under repeats can come later if the result is close.
