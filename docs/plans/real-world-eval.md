# Plan: the bot on real pull requests (A: precision, B: did people act on it)

Proposed 2026-11-06, not yet approved. This replaces row 2 (reruns) as the next eval step. The
reruns are stopped, so this step gets the Groq quota. The repeats report stays in the code for
later.

**Goal:** answer, with evidence anyone can click, the questions an interviewer will have:
- **A:** does the deployed bot find real problems in real code, and how often is it wrong?
- **B:** do its comments land where human reviewers also saw a problem?

**Repos:** `encode/starlette`, `encode/httpx`, `pydantic/pydantic-settings`. They're libraries this
service itself runs on. Permissive licenses, small, active. `pallets/click` is the backup if one
of them has too few suitable PRs.

**Done when:**
- 30 real PRs (10 per repo), chosen by a fixed rule and a seeded random sample before the bot
  sees any of them, have been reviewed by the deployed bot.
- Every inline comment is judged by hand, and the outcome checks for B have run.
- `realworld/report.md` gives precision, the wrong-comment rate, B's match rate against a base
  rate, and cost and latency from production, each with an interval or a test.
- The README leads its results with this.
- No change to the bot (agent v3 frozen, prompts and budgets as in prod).

## 1. Choosing the PRs fairly [`realworld/select.py`]
**The rule**, fixed and committed before any replay:
- merged, opened in the last 2 years, by a person (no bots, no release or version-bump PRs);
- it changes at least one `.py` file outside the tests and docs;
- the replayed version (below) changes at most 300 lines in at most 10 files, to stay inside the
  per-PR token budget;
- it got at least one line comment from a reviewer other than the author, so B has something to
  compare with. This is a selection bias, and the report states it: these are PRs that drew
  review attention.

**Which version gets replayed: the one the reviewers first saw.** That's the PR's last commit
before its first human review. Replaying the final merged version would hide exactly the
problems the reviewers caught, and B needs those.

**The sample:**
- **Who picks:** a seeded random sample of 10 per repo from the PRs that qualify, so nobody hand-picks.
- **A second, disjoint sample** of 10 per repo is drawn now and kept unused. It's the fresh test
  set for row 4, after any change to the bot, so the new bot is never judged on PRs that shaped it.
- **Pre-registered:** both lists are written to `realworld/prs.json` and committed before any
  replay.

## 2. Replaying them [`realworld/replay.py`] (outward-facing, so with safeguards)
**Your one-time setup** (step-by-step instructions when we get there):
- fork the 3 repos to `amn-00`;
- install the GitHub App on the 3 forks;
- install the GitHub CLI and log in (`gh auth login`).

**For each PR, the script:**
1. fetches the upstream PR's commits (`pull/N/head`, public, read-only);
2. pushes two branches to **your fork**: `replay/<n>-base`, at the merge base, and `replay/<n>`,
   at the replayed commit;
3. opens a PR between them **in your fork**, with `gh pr create --repo amn-00/<fork>`.

GitHub then sends the webhook to Render, and the deployed bot reviews it, exactly as in
production.

**Safeguards (tested):**
- **Forks only:** the script refuses any target repo not owned by `amn-00`. `gh` defaults to
  opening PRs against the upstream, which would spam the maintainers, so `--repo` is always
  explicit.
- **No links to the original PR** in titles or bodies. A link like `encode/starlette#123` would
  add a "referenced this pull request" notice to the maintainers' PR. The body says "Replay k
  of 30 for an evaluation. Not for merging." The mapping lives only in `prs.json`.
- **Paced at 4 PRs a day** (`--max 4`), so reviews don't hit the daily budgets and get deferred
  to midnight. 30 PRs take about 8 days, run once a day.
- **No Actions in the forks.** GitHub leaves Actions off in forks by default. Keep it off, so
  the bot sees no CI results, as for any PR without CI.

## 3. Collecting [`realworld/collect.py`]
For each replayed PR:
- **From Neon** (read-only, over the same verified TLS): the run, every candidate with its
  verdict and evidence, agent steps, cost, and the run's start and finish times.
- **From GitHub:** the posted review and its inline comments, with links.

One JSON file per PR goes in `realworld/results/`.

## 4. Judging A, by hand [`realworld/labels.csv`]
The script writes one row per inline comment, with its link. You fill in:
- `real`: a genuine problem a maintainer would want to know about;
- `wrong`: the claim is false, or it misreads the code;
- `nit`: true, but not worth a comment.

There's also a short note column. Judge each comment by reading the code, before looking at
B's results for that PR, so B can't bias the labels.

**The limit, stated in the report:** there's no ground truth for what the bot missed on real
code, so there's no recall here. Recall comes from the synthetic benchmark only.

## 5. B: where humans saw a problem too [`realworld/outcomes.py`]
For each inline comment, from the upstream PR's public history:
- **Reviewer match:** a human line comment on the original PR within ±3 lines in the same file.
- **Changed before merge:** the flagged line was changed or removed between the replayed
  version and the merged version (`git diff`).

**The base rate:** the share of *all* lines the replayed version adds that changed before merge.
The test (Fisher's exact test, the same as the eval reports) asks whether the bot's flagged
lines change more often than lines picked at random.

**A match isn't proof the human saw the same issue.** So each match gets a hand check (a
`same_issue` column): does the human comment or change address what the bot said? The report
gives both numbers, automatic matches and confirmed same-issue matches.

## 6. The report [`realworld/report.py`, `realworld/report.md`]
- **A:** precision (real / inline comments), the wrong rate and the nit rate, with Wilson
  intervals, per repo and in total. Plus how many PRs got no inline comment, since a quiet bot is
  fine on good PRs.
- **B:** the match rates against the base rate, Fisher's p, and the confirmed same-issue count.
- **Production numbers:** cost per PR, time from webhook to review, and agent verdict counts.
- **Every comment** in a table, with its label, its link and the B result.
- **The limits:** 30 PRs; libraries rather than applications; PRs that drew review attention;
  judged by the person who built the bot; no recall.

## Tests
- **Selection:** the filter rules (bot authors, docs-only, size), seeded sampling is
  reproducible, and the two samples are disjoint.
- **Replay:** a non-`amn-00` repo is refused, and the PR body contains no upstream reference.
- **Outcomes:** line matching within ±3, "changed before merge" through rename and line-shift
  cases, and the base rate.
- **Report:** stats against hand-computed values (the existing `wilson` and `fisher_one_sided`
  are reused).

## Files
```
realworld/select.py, replay.py, collect.py, outcomes.py, report.py
realworld/prs.json            the two pre-registered samples (committed before any replay)
realworld/results/*.json      collected runs
realworld/labels.csv          your judgments
realworld/report.md
tests/test_realworld_*.py
README.md, docs/decisions.md
```
- **The app is untouched:** no change to `app/`, `prompts/` or the prod settings.
- **New external tool:** the GitHub CLI, on your machine only, not a Python dependency.
- **No new Python dependencies.** The scripts use stdlib, `httpx` and `sqlalchemy`, already
  in the project.

## Effort and time
- **Me:** about 2 days for the scripts and tests, in two parts. Selection and replay first, so
  the quota starts working; collection, outcomes and the report while the replays run.
- **You:**
  - the setup (about 30 minutes);
  - one command a day for about 8 days;
  - judging about 30–60 comments (about 2 hours).
- **Groq:** the deployed bot's normal daily budgets. Don't run evals on the same days.

## Decisions for you
1. **The GitHub CLI** (`gh`) to open the PRs. I'd say yes: it handles login safely. The
   alternative is a personal access token in an env var.
2. **30 PRs, 10 per repo,** plus a reserved 30 for after bot changes. I'd say yes; 45 would
   take about 12 days.
3. **Replay the version the reviewers first saw,** not the final merged version. I'd say yes:
   B depends on it.
4. **4 PRs a day.** I'd say yes: it keeps reviews inside the daily budgets.
