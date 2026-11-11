# Plan: the bot on real pull requests (A: precision, B: did people act on it)

Proposed 2026-11-06, approved 2026-11-11 with the four decisions at the bottom. Three changes
made at approval are marked *(amended)*: one new commit per replay, a budget check before each
PR, and budget-limited reviews left out of the scores. The "caught what reviewers caught"
number in section 5 was added the same day. This replaces row 2 (reruns) as the next eval step. The reruns are stopped, so this step gets the Groq quota. The repeats report stays in
the code for later.

**Goal:** answer, with evidence anyone can click, the questions an interviewer will have:
- **A:** does the deployed bot find real problems in real code, and how often is it wrong?
- **B:** do its comments land where human reviewers also saw a problem?

**Repos:** `Kludex/starlette` (it moved from `encode/starlette`, found while building) and
`pydantic/pydantic-settings`. They're libraries this service itself runs on. Permissive
licenses, small, active.
- *(amended 2026-11-11, after the first draw)* **Two repos, not three.** The plan also named
  `encode/httpx`, with `pallets/click` as the backup. Each repo needs 20 PRs that pass the rule
  below. httpx had 0, click 9 and `Kludex/uvicorn` 7: most of their merged PRs have no line
  comment from a human reviewer. Starlette had 25 and pydantic-settings 29, so each gives 15.

**Done when:**
- 30 real PRs (15 per repo), chosen by a fixed rule and a seeded random sample before the bot
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
  review attention;
- *(amended)* the replayed diff has no link to a GitHub issue or PR, in any repo
  (`github.com/<owner>/<repo>/issues/N`, `/pull/N` or `<owner>/<repo>#N`). The bot's comments
  are model text, and a comment that repeated such a link could add a cross-reference on that
  issue.

**Which version gets replayed: the one the reviewers first saw.** That's the PR's last commit
before its first human review. Replaying the final merged version would hide exactly the
problems the reviewers caught, and B needs those.

**The sample:**
- **Who picks:** a seeded random sample of 15 per repo from the PRs that qualify, so nobody hand-picks.
- **A second, disjoint sample** is drawn now and kept unused: the PRs that qualify beyond the
  first 15, which is 10 from Starlette and 14 from pydantic-settings. It's the fresh test
  set for row 4, after any change to the bot, so the new bot is never judged on PRs that shaped it.
- *(amended)* **Checked before it's committed:** the script prints each sampled PR's lane-token
  estimate, from the app's own `estimate_call_tokens`. The 300-line limit is a guess at what
  fits the 50K per-PR budget. If many sampled PRs are over it, the limit is tightened and the
  sample drawn again, before the bot has seen anything.
- **Pre-registered:** both lists are written to `realworld/prs.json` and committed before any
  replay.

## 2. Replaying them [`realworld/replay.py`] (outward-facing, so with safeguards)
**Your one-time setup** (step-by-step instructions when we get there):
- fork the 3 repos to `amn-00`;
- install the GitHub App on the 3 forks;
- install the GitHub CLI and log in (`gh auth login`).

**For each PR, the script:**
1. fetches the upstream PR's commits (`pull/N/head`, public, read-only) into a local clone;
2. *(amended)* makes **one new commit** holding the replayed version's files, on top of the
   merge base, with the message "Replay k of 30";
3. pushes two branches to **your fork**: `replay/<k>-base`, at the merge base, and `replay/<k>`,
   at the new commit (k is the PR's place in the sample, 1 to 30);
4. *(amended)* opens a PR between them **in your fork**, through GitHub's API with the GitHub
   CLI's login (`gh auth token`), not `gh pr create`: one client for the reads and the write;
5. *(amended)* waits for the bot's run to finish before starting the next PR.

After step 4, GitHub sends the webhook to Render, and the deployed bot reviews the PR, exactly
as in production.

**Safeguards (tested):**
- **Forks only:** the script refuses any target repo not owned by `amn-00`, before every push
  and before opening a PR. A PR opened against the upstream would spam the maintainers.
- **No links to the original PR** in titles or bodies. A link like `Kludex/starlette#123` would
  add a "referenced this pull request" notice to the maintainers' PR. The body says "Replay k
  of 30 for an evaluation. Not for merging." The mapping lives only in `prs.json`.
- *(amended)* **No upstream commit messages.** The original commits' messages often contain
  `#123` or `Fixes #123`. I haven't verified whether GitHub adds a "referenced this" entry
  upstream when such a commit is pushed to a fork, so the replay never pushes them. Step 2's
  commit has the same files on the same base, so the PR's diff is identical. The bot reads the
  diff and the files, never the commit messages.
- *(amended)* **At most 4 PRs a day (`--max 4`), one at a time, with a budget check before
  each.**
  - **Why:** a review that runs short of a daily budget is still posted, with lane calls "not
    run (daily token budget)" or findings "not verified (agent daily token budget)". That
    would count against the bot for a quota reason. The agent's 70K a day is about 12
    findings (5.8K each in the v3 eval), and one PR may use about 30K of it.
  - **The check:** before opening each PR, the script reads today's `daily_token_usage` from
    Neon and stops for the day unless the lanes have 50K left on each model and the agent has
    30K left. Those are the most one attempt can spend (`PR_TOKEN_BUDGET`,
    `AGENT_PR_TOKEN_BUDGET`, imported from the app). It's the eval runner's pre-flight, for
    prod.
  - **The pace:** 4 is the cap. Expect about 3 on a typical day, so about 10 days in all.
- *(amended)* **Neon is read-only here.** The scripts take the URL from `PROD_DATABASE_URL`,
  never from `.env`, and set the session read-only, so a bug can't write to the prod database.
- **No Actions in the forks.** GitHub leaves Actions off in forks by default. Keep it off, so
  the bot sees no CI results, as for any PR without CI.

## 3. Collecting [`realworld/collect.py`]
For each replayed PR:
- **From Neon** (read-only, over the same verified TLS): the run, every candidate with its
  verdict and evidence, agent steps, cost, and the run's start and finish times.
- **From GitHub:** the posted review and its inline comments, with links.

One JSON file per PR goes in `realworld/results/`.

*(amended)* **Budget-limited reviews aren't scored.** A review is budget-limited if a lane call
wasn't run for the daily budget, a finding wasn't verified for the agent's daily budget, or the
run didn't complete: the eval runner's rule for "incomplete". The report lists those PRs and
leaves them out of A and B. The budget check in section 2 should keep the count at zero; this
is the safety net. Skips for the per-PR budget or the time limit are the bot's normal behaviour:
they stay in, and the report counts them. Collection also reports any upstream link found in a
posted review.

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

**The other direction: did the bot catch what the reviewers caught?** *(added 2026-11-11)*
- **The data:** every human line comment made on the replayed version (its
  `original_commit_id` is the replayed commit, so its line numbers are the ones the bot saw).
  The script writes them to `realworld/reviewer_comments.csv`, and you mark each one `problem`
  (a real code problem) or not (style, a question, praise, a design discussion).
- **The number:** of the comments marked `problem`, how many have a bot inline comment within
  ±3 lines in the same file, with a Wilson interval and the same `same_issue` hand check.
- **What it is and isn't:** "the bot flagged M of the N problems reviewers raised". It isn't
  recall: reviewers miss things too. It's the closest thing to recall that real code allows.

## 6. The report [`realworld/report.py`, `realworld/report.md`]
- **A:** precision (real / inline comments), the wrong rate and the nit rate, with Wilson
  intervals, per repo and in total. Plus how many PRs got no inline comment, since a quiet bot is
  fine on good PRs.
- **B:** the match rates against the base rate, Fisher's p, and the confirmed same-issue count.
  Then the other direction: how many of the problems the reviewers raised the bot also flagged.
- **Production numbers:** cost per PR, time from webhook to review, agent verdict counts, and
  how many reviews hit the per-PR budget or the time limit.
- **Every comment** in a table, with its label, its link and the B result.
- **The limits:** 30 PRs; libraries rather than applications; PRs that drew review attention;
  judged by the person who built the bot; no recall.

## Tests
- **Selection:** the filter rules (bot authors, docs-only, size, upstream links in the diff),
  seeded sampling is reproducible, and the two samples are disjoint.
- **Replay:**
  - a non-`amn-00` repo is refused;
  - the PR title, the PR body and the commit message contain no upstream reference;
  - the budget check stops when the lanes or the agent are short, and the session can't write.
- **Collection:** a review with a daily-budget skip or an unverified finding is flagged
  budget-limited; one with only a per-PR budget skip isn't.
- **Outcomes:** line matching within ±3, "changed before merge" through rename and line-shift
  cases, the base rate, and the reviewers' problems matched in the other direction.
- **Report:** stats against hand-computed values (the existing `wilson` and `fisher_one_sided`
  are reused).

## Files
```
realworld/select.py, replay.py, collect.py, outcomes.py, report.py
realworld/prs.json            the two pre-registered samples (committed before any replay)
realworld/results/*.json      collected runs
realworld/labels.csv          your judgments
realworld/reviewer_comments.csv   the reviewers' line comments, marked problem or not
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
  - *(amended)* one command a day for about 10 days. I expect it to take 15–30 minutes,
    because it waits for each review. It needs the Neon URL on the clipboard, as in
    `docs/deploy.md`;
  - judging about 30–60 comments (about 2 hours);
  - marking the reviewers' line comments as a problem or not (about 1 hour).
- **Groq:** the deployed bot's normal daily budgets. Don't run evals on the same days.

## Decisions (made 2026-11-11)
1. **The GitHub CLI** (`gh`) opens the PRs: it handles login safely. Not a personal access
   token in an env var.
2. **30 PRs, 10 per repo,** plus a reserved 30 for after bot changes. *(Changed the same day,
   after the first draw: 15 per repo from two repos, plus a reserved 24. See "Repos" above.)*
3. **The version the reviewers first saw** is replayed, not the final merged version: B depends
   on it.
4. **At most 4 PRs a day,** with the budget check in section 2 deciding each day's real number.
