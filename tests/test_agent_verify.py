import time

import httpx
import pytest
from sqlalchemy import func, select

from app.agent import verify
from app.agent.verify import (
    AGENT_DAILY_TOKEN_BUDGET,
    NOT_VERIFIED_DAILY_BUDGET,
    NOT_VERIFIED_PR_BUDGET,
    NOT_VERIFIED_TIME,
    verify_findings,
)
from app.db.models import AgentStep, BudgetPurpose, Finding, ReviewRun
from app.db.queries import add_daily_tokens, daily_tokens_used, get_or_create_run, record_findings
from app.github.http import NonRetryableError
from app.github.webhooks import PullRequestPayload
from app.pipeline.lanes import Candidate
from app.pipeline.orchestrator import utc_today
from tests.factories import (
    BUGGY_CONTENT,
    BUGGY_FILE,
    INSTALLATION_ID,
    REPO,
    SHA_1,
    make_chunk,
    pr_payload,
    repo_tarball,
)

MODEL = "openai/gpt-oss-120b"
TARBALL_URL = f"/repos/{REPO}/tarball/{SHA_1}"


def candidate(line: int, title: str) -> Candidate:
    return Candidate("correctness", "v3", BUGGY_FILE, line, line, "bug", "high", title, "e", None)


@pytest.fixture
def tarball(github_api):
    return github_api.get(TARBALL_URL).mock(
        return_value=httpx.Response(200, content=repo_tarball({BUGGY_FILE: BUGGY_CONTENT}))
    )


async def saved_findings(sessionmaker, *candidates: Candidate) -> tuple[int, list]:
    async with sessionmaker() as session:
        run = await get_or_create_run(session, PullRequestPayload.model_validate(pr_payload()))
        ids = await record_findings(session, run.id, list(candidates), True)
    return run.id, list(zip(ids, candidates, strict=True))


async def run_agent(sessionmaker, github, groq, run_id, findings, deadline_in=600):
    return await verify_findings(
        groq,
        github,
        sessionmaker,
        MODEL,
        INSTALLATION_ID,
        REPO,
        SHA_1,
        run_id,
        findings,
        [make_chunk()],
        time.monotonic() + deadline_in,
    )


async def rows(sessionmaker, model):
    async with sessionmaker() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_each_finding_gets_a_verdict_and_every_step_is_stored(
    sessionmaker, github, groq, tarball
):
    groq.verdicts_by_title = {"Real": "confirmed", "Guarded": "dropped"}
    run_id, findings = await saved_findings(
        sessionmaker, candidate(3, "Real"), candidate(2, "Guarded")
    )

    result = await run_agent(sessionmaker, github, groq, run_id, findings)

    assert [(c.candidate.title, c.verdict) for c in result.checks] == [
        ("Real", "confirmed"),
        ("Guarded", "dropped"),
    ]
    assert result.checks[0].evidence == [
        {"path": BUGGY_FILE, "line": 3, "why": "the finding's line"}
    ]
    assert tarball.call_count == 1  # one snapshot shared by every finding
    assert {c["model"] for c in groq.agent_calls} == {MODEL}

    saved = await rows(sessionmaker, Finding)
    assert [(f.verdict, f.verdict_reason) for f in saved] == [
        ("confirmed", "checked: Real"),
        ("dropped", "checked: Guarded"),
    ]
    steps = await rows(sessionmaker, AgentStep)
    assert sorted((s.finding_id, s.tool) for s in steps) == sorted(
        (finding_id, "submit_verdict") for finding_id, _ in findings
    )


async def test_agent_usage_goes_to_the_run_and_the_agents_daily_budget(
    sessionmaker, github, groq, tarball
):
    run_id, findings = await saved_findings(sessionmaker, candidate(3, "Real"))

    result = await run_agent(sessionmaker, github, groq, run_id, findings)

    assert (result.usage.tokens_in, result.usage.tokens_out, result.usage.llm_calls) == (200, 30, 1)
    [run] = await rows(sessionmaker, ReviewRun)
    assert (run.tokens_in, run.tokens_out, run.llm_calls) == (200, 30, 1)
    assert run.cost_usd > 0
    async with sessionmaker() as session:
        today = utc_today()
        assert await daily_tokens_used(session, MODEL, today, BudgetPurpose.AGENT) == 230
        assert await daily_tokens_used(session, MODEL, today, BudgetPurpose.PIPELINE) == 0


async def test_used_up_daily_budget_verifies_nothing_and_skips_the_download(
    sessionmaker, github, groq, tarball
):
    async with sessionmaker() as session:
        await add_daily_tokens(
            session, MODEL, utc_today(), BudgetPurpose.AGENT, AGENT_DAILY_TOKEN_BUDGET
        )
    run_id, findings = await saved_findings(sessionmaker, candidate(3, "Real"))

    result = await run_agent(sessionmaker, github, groq, run_id, findings)

    assert [(c.verdict, c.reason) for c in result.checks] == [(None, NOT_VERIFIED_DAILY_BUDGET)]
    assert tarball.call_count == 0 and groq.agent_calls == []
    [saved] = await rows(sessionmaker, Finding)
    assert (saved.verdict, saved.verdict_reason) == (None, NOT_VERIFIED_DAILY_BUDGET)


async def test_the_pr_budget_stops_starting_new_findings(
    sessionmaker, github, groq, tarball, monkeypatch
):
    # Room for one finding's estimate; after it has spent 230 tokens, not for another.
    monkeypatch.setattr(verify, "AGENT_PR_TOKEN_BUDGET", verify.FINDING_ESTIMATE_TOKENS + 100)
    monkeypatch.setattr(verify, "MAX_PARALLEL_AGENTS", 1)
    run_id, findings = await saved_findings(
        sessionmaker, candidate(3, "First"), candidate(2, "Second")
    )

    result = await run_agent(sessionmaker, github, groq, run_id, findings)

    assert [(c.verdict, c.reason) for c in result.checks] == [
        ("confirmed", "checked: First"),
        (None, NOT_VERIFIED_PR_BUDGET),
    ]


async def test_no_finding_starts_too_close_to_the_deadline(sessionmaker, github, groq, tarball):
    run_id, findings = await saved_findings(sessionmaker, candidate(3, "Real"))

    result = await run_agent(sessionmaker, github, groq, run_id, findings, deadline_in=10)

    assert [(c.verdict, c.reason) for c in result.checks] == [(None, NOT_VERIFIED_TIME)]
    assert groq.agent_calls == []


async def test_without_a_snapshot_the_agent_still_runs(sessionmaker, github, groq, github_api):
    github_api.get(TARBALL_URL).mock(return_value=httpx.Response(404))
    run_id, findings = await saved_findings(sessionmaker, candidate(3, "Real"))

    result = await run_agent(sessionmaker, github, groq, run_id, findings)

    assert result.checks[0].verdict == "confirmed"  # from the excerpt, which needs no snapshot


async def test_a_rejected_api_key_stops_the_agent(sessionmaker, github, groq, tarball):
    async def rejected(*args, **kwargs):
        raise NonRetryableError("Groq 401")

    groq.complete_tools = rejected
    run_id, findings = await saved_findings(sessionmaker, candidate(3, "Real"))

    with pytest.raises(NonRetryableError):
        await run_agent(sessionmaker, github, groq, run_id, findings)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(AgentStep)) == 0
