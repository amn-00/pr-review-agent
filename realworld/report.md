# The bot on real pull requests

Generated 2026-11-21 15:02 UTC from `realworld/`.

**Read this first.**
- **A small sample:** 30 real pull requests from 2 projects. Every rate below
  comes with a 95% interval, and the intervals are wide.
- **Libraries, not applications,** and only PRs that drew a human reviewer's line comment. Such
  PRs may be harder or easier than the average PR.
- **Judged by the person who built the bot.** The labels (`real`, `wrong`, `nit`) are one
  person's reading of the code. Part B doesn't depend on them: it compares the bot with what
  the projects' own reviewers did.
- **No recall.** On real code nobody knows the full list of problems, so this report can't say
  what share of them the bot finds. "Caught what reviewers caught" is the closest it gets, and
  reviewers miss things too.
- **The bot was not changed for this evaluation,** and it had never seen these PRs. They were
  chosen by a fixed rule and a seeded random draw before the first replay (`prs.json`).

## Coverage

- Replayed: 17 of 30 sampled PRs; collected: 17.
- Scored: 17. Left out as budget-limited (a daily token budget cut the review short, or the run didn't complete): 0.

## A: are the bot's comments right?

**10 comment(s) aren't labelled yet** and aren't counted.

| Repo | PRs | PRs with no inline comment | Inline comments | Real | Wrong | Nit | Precision (real / labelled) | Wrong rate |
|---|---|---|---|---|---|---|---|---|
| Kludex/starlette | 9 | 4 | 6 | 0 | 0 | 0 | n/a | n/a |
| pydantic/pydantic-settings | 8 | 5 | 4 | 0 | 0 | 0 | n/a | n/a |
| All | 17 | 9 | 10 | 0 | 0 | 0 | n/a | n/a |

`real`: a genuine problem a maintainer would want to know about. `wrong`: the claim is false or misreads the code. `nit`: true, but not worth a comment. A PR with no inline comment isn't a failure: a quiet bot is right on a clean PR.

## B: does the bot agree with the human reviewers?

**Bot comments near a reviewer's comment** (same file, within 3 lines, on the version both saw): 1/10: 2–40%. Checked by hand to be about the same issue: 0 (1 pair(s) not judged yet).

**Did the lines the bot flagged change before the merge?** Of the lines these PRs added, the bot flagged 18.

| Added lines | Changed or removed before merge |
|---|---|
| Flagged by the bot | 9/18: 29–71% |
| Not flagged (the base rate) | 244/1010: 22–27% |

If the bot's lines were picked at random, the chance of this many or more changing is **p = 0.017** (Fisher's exact test, one-sided). Lines in one PR aren't independent, so read p as a rough guide. A changed line isn't proof the bot was right: the hand labels in A say that.

**Did the bot catch what the reviewers caught?** The reviewers left 20 line comment(s) on the replayed versions; 0 are marked as a real code problem (the rest are style, questions or design talk); 20 aren't marked yet. The bot commented within 3 lines of n/a of those problems, and 0 of the matches are confirmed by hand to be the same issue. This isn't recall: reviewers miss things too.

## Production numbers

These are the deployed bot's own figures (Render free tier, Groq free tier), one PR at a time.

- **Cost per PR at Groq's list prices:** $0.0079 on average, $0.0155 at most.
- **Time from the PR being opened to the review:** 5.2 minutes (median), 16.6 at most. Most of it is waiting for the free tier's per-minute token limit.
- **The agent's verdicts on the candidates:** 28 confirmed, 24 dropped, 9 uncertain, 9 not verified.
- **Reviews that hit the per-PR token budget or the time limit:** 3 of 17. The bot then reviews source files first and lists what it skipped.

## Every inline comment

| Replay | Comment | What it says | Label | Near a reviewer's comment | Changed before merge |
|---|---|---|---|---|---|
| 2 | [`pydantic_settings/sources/providers/env.py:370`](https://github.com/amn-00/pydantic-settings/pull/1#discussion_r4541360093) | medium · bug · Swallows JSON decoding errors silently | — | no | no |
| 2 | [`tests/test_settings.py:3972`](https://github.com/amn-00/pydantic-settings/pull/1#discussion_r4541360104) | medium · bug · Invalid usage of strict parameter in BaseSettings subclass | — | no | no |
| 3 | [`starlette/middleware/gzip.py:183`](https://github.com/amn-00/starlette/pull/2#discussion_r4541444767) | medium · bug · Missing update of started flag may cause duplicate initial messages | — | yes | yes |
| 5 | [`starlette/routing.py:401`](https://github.com/amn-00/starlette/pull/3#discussion_r4589049117) | medium · bug · Potential UnboundLocalError for non-HTTP/WebSocket scopes | — | no | no |
| 7 | [`.github/workflows/main.yml:17`](https://github.com/amn-00/starlette/pull/4#discussion_r4602292126) | medium · bug · Unsupported Python version may cause workflow failures | — | no | no |
| 8 | [`pydantic_settings/main.py:176`](https://github.com/amn-00/pydantic-settings/pull/4#discussion_r4616448122) | high · bug · New constructor argument `_env_prefix_target` not validated or stored | — | no | no |
| 11 | [`starlette/middleware/exceptions.py:22`](https://github.com/amn-00/starlette/pull/6#discussion_r4630912292) | medium · security · Unvalidated exception handlers may allow execution of attacker-controlled code | — | no | no |
| 11 | [`tests/test_exceptions.py:56`](https://github.com/amn-00/starlette/pull/6#discussion_r4630912305) | medium · bug · Catch-all handlers raise NotImplementedError instead of returning a response | — | no | yes |
| 14 | [`pydantic_settings/sources/providers/aws.py:70`](https://github.com/amn-00/pydantic-settings/pull/7#discussion_r4642891802) | medium · bug · Assumes 'SecretString' key always present in AWS Secrets Manager response | — | no | no |
| 17 | [`starlette/testclient.py:292`](https://github.com/amn-00/starlette/pull/9#discussion_r4653274335) | medium · bug · ASGI `client` field should be a tuple, not a list | — | no | yes |

