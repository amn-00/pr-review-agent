"""The eval runner end to end: the real review job and test database, scripted Groq replies."""

import json

from sqlalchemy import func, select

from app.agent import loop, verify
from app.agent.loop import AGENT_PROMPT_VERSION
from app.agent.verify import AGENT_DAILY_TOKEN_BUDGET
from app.config import get_settings
from app.db.models import BudgetPurpose, ReviewRun
from app.db.queries import add_daily_tokens
from app.llm.groq import LLMError
from app.pipeline.orchestrator import PIPELINE_DAILY_TOKEN_BUDGET, utc_today
from evals.dataset import load_case
from evals.report import load_results, render
from evals.run import (
    MODES,
    RecordingGroq,
    agent_cap,
    copy_pipeline_results,
    over_budget,
    read_result,
    run_one,
    run_session,
    write_result,
)
from tests.conftest import FakeGroq
from tests.factories import lane_finding
from tests.test_evals_dataset import write_case

settings = get_settings()

CASE_TOML = """kind = "bug"
title = "Add average"

[[expect]]
file = "app/calc.py"
lines = [3, 3]
note = "division by zero for an empty list"
"""
BASE = {"app/calc.py": "def average(values):\n    total = sum(values)\n", "app/other.py": "x = 1\n"}
HEAD = {"app/calc.py": BASE["app/calc.py"] + "    return total / len(values)\n"}
PUSH2 = {"app/other.py": "x = 2\n"}


def tiny_case(tmp_path):
    write_case(tmp_path / "c", CASE_TOML, BASE, HEAD, PUSH2)
    return load_case(tmp_path / "c")


def scripted(groq: FakeGroq) -> RecordingGroq:
    groq.findings_by_lane = {"correctness": [lane_finding(3, 3)]}
    return RecordingGroq(groq)


async def test_a_case_runs_in_every_mode_and_scores(tmp_path, sessionmaker, groq):
    case, results = tiny_case(tmp_path), tmp_path / "results"
    agent_setting = settings.agent_enabled

    await run_session([case], list(MODES), sessionmaker, scripted(groq), results, 0)

    pipeline, agent, repush = (read_result(results / f"c.{mode}.json") for mode in MODES)
    assert [r["status"] for r in (pipeline, agent, repush)] == ["complete"] * 3
    assert settings.agent_enabled == agent_setting  # restored after each run

    # Pipeline only: the lanes paid, the finding was posted unverified.
    assert pipeline["run"]["agent_enabled"] is False
    # Lane calls run in parallel, so they're recorded in whatever order they finish.
    lanes = sorted(c["kind"] for c in pipeline["calls"])
    assert lanes == ["correctness", "maintainability", "security"]
    [posted] = [f for f in pipeline["findings"] if f["posted"]]
    assert posted["verdict"] is None

    # With the agent: every lane call came from the cache, only the agent paid.
    assert agent["run"]["cached_calls"] == 3
    assert {c["kind"] for c in agent["calls"]} == {"agent"}
    [confirmed] = [f for f in agent["findings"] if f["posted"]]
    assert confirmed["verdict"] == "confirmed" and confirmed["agent_steps"]

    # Re-push: calc.py's chunk from the cache, only other.py's new chunk paid.
    assert (repush["run"]["cached_calls"], repush["run"]["llm_calls"]) == (3, 3)
    assert repush["run"]["tokens_saved"] > 0

    report = render("t", [case], load_results(results))
    assert "| Bugs caught | 1/1 (100%) | 1/1 (100%) |" in report
    assert "synthetic benchmark" in report
    assert "**One sample per case and mode, with no repeat runs.**" in report
    assert "**A difference of one finding between two columns is within noise**" in report


async def test_finished_runs_are_not_run_again_until_the_case_changes(tmp_path, sessionmaker, groq):
    case, results = tiny_case(tmp_path), tmp_path / "results"
    recording = scripted(groq)
    await run_session([case], ["pipeline", "agent"], sessionmaker, recording, results, 0)
    calls = (len(groq.calls), len(groq.agent_calls))

    await run_session([case], ["pipeline", "agent"], sessionmaker, recording, results, 0)
    assert (len(groq.calls), len(groq.agent_calls)) == calls

    # Editing the case gives a new head SHA: its results are stale and run again.
    (tmp_path / "c" / "head" / "app" / "calc.py").write_text(
        HEAD["app/calc.py"] + "# changed\n", encoding="utf-8"
    )
    edited = load_case(tmp_path / "c")
    await run_session([edited], ["pipeline"], sessionmaker, recording, results, 0)
    assert read_result(results / "c.pipeline.json")["head_sha"] == edited.target("pipeline")[1]


async def test_incomplete_run_runs_again_reusing_what_it_paid_for(tmp_path, sessionmaker, groq):
    case, results = tiny_case(tmp_path), tmp_path / "results"
    recording = scripted(groq)
    await run_session([case], ["pipeline"], sessionmaker, recording, results, 0)
    path = results / "c.pipeline.json"
    first = read_result(path)
    path.write_text(json.dumps(first | {"status": "incomplete"}), encoding="utf-8")

    await run_session([case], ["pipeline"], sessionmaker, recording, results, 0)

    again = read_result(path)
    assert again["status"] == "complete"
    assert again["earlier_calls"] == first["calls"]  # still counted in the cost
    assert (again["run"]["llm_calls"], again["run"]["cached_calls"]) == (0, 3)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ReviewRun)) == 1


async def test_a_run_that_doesnt_fit_todays_budget_waits(tmp_path, sessionmaker, groq, capsys):
    case, results = tiny_case(tmp_path), tmp_path / "results"
    async with sessionmaker() as session:
        await add_daily_tokens(
            session,
            settings.groq_model,
            utc_today(),
            BudgetPurpose.PIPELINE,
            PIPELINE_DAILY_TOKEN_BUDGET,
        )

    await run_session([case], ["pipeline", "agent"], sessionmaker, scripted(groq), results, 0)

    assert groq.calls == []
    assert not (results / "c.pipeline.json").exists()
    output = capsys.readouterr().out
    assert "c [pipeline]: left for another day" in output
    assert "c [agent]: waiting for the pipeline run" in output


async def spent_today(sessionmaker, purpose: BudgetPurpose, tokens: int) -> None:
    async with sessionmaker() as session:
        await add_daily_tokens(session, settings.groq_model, utc_today(), purpose, tokens)


async def test_a_higher_agent_budget_lets_the_agent_use_the_days_room(
    tmp_path, sessionmaker, groq, capsys
):
    case, results = tiny_case(tmp_path), tmp_path / "results"
    recording = scripted(groq)
    await run_session([case], ["pipeline"], sessionmaker, recording, results, 0)
    # The agent already spent 68K today: the default 70K has no room for one more finding.
    await spent_today(sessionmaker, BudgetPurpose.AGENT, 68_000)

    await run_session([case], ["agent"], sessionmaker, recording, results, 0)
    assert "c [agent]: left for another day" in capsys.readouterr().out

    await run_session([case], ["agent"], sessionmaker, recording, results, 0, agent_budget=160_000)

    agent = read_result(results / "c.agent.json")
    # Complete with a verdict, so the job's own check used the raised budget too...
    assert agent["status"] == "complete"
    assert [f["verdict"] for f in agent["findings"]] == ["confirmed"]
    # ...and only for that run: production's default is back.
    assert verify.AGENT_DAILY_TOKEN_BUDGET == AGENT_DAILY_TOKEN_BUDGET == 70_000


async def test_lanes_and_agent_never_plan_past_groqs_daily_limit(sessionmaker):
    # 175K of Groq's 200K used today; the runner keeps 20K headroom, so 5K is left for both.
    await spent_today(sessionmaker, BudgetPurpose.PIPELINE, 100_000)
    await spent_today(sessionmaker, BudgetPurpose.AGENT, 75_000)
    model = settings.groq_model

    agent = await over_budget(sessionmaker, {model: 17_000}, BudgetPurpose.AGENT, 160_000)
    lanes = await over_budget(sessionmaker, {model: 6_000}, BudgetPurpose.PIPELINE, 160_000)
    small = await over_budget(sessionmaker, {model: 5_000}, BudgetPurpose.PIPELINE, 160_000)

    assert agent == [f"{model} needs 17000, 5000 left"]  # its own budget has 85K left
    assert lanes == [f"{model} needs 6000, 5000 left"]  # its own budget has 10K left
    assert small == []
    # The job's agent budget is lowered to what the lanes leave under the limit.
    assert await agent_cap(sessionmaker, 160_000) == 80_000
    assert await agent_cap(sessionmaker, AGENT_DAILY_TOKEN_BUDGET) == 70_000


async def test_job_deferred_for_the_daily_budget_is_incomplete(tmp_path, sessionmaker, groq):
    case = tiny_case(tmp_path)
    async with sessionmaker() as session:
        for model in {settings.groq_model, settings.groq_small_model}:
            await add_daily_tokens(
                session, model, utc_today(), BudgetPurpose.PIPELINE, PIPELINE_DAILY_TOKEN_BUDGET
            )

    result = await run_one(sessionmaker, scripted(groq), case, "pipeline", [])

    assert result["status"] == "incomplete"
    assert result["problems"][0].startswith("deferred by the job")


class FlakyGroq(FakeGroq):
    async def complete_json(self, model, messages, schema_name, schema, deadline):
        if schema_name == "security_findings":
            raise LLMError("Groq returned 500")
        return await super().complete_json(model, messages, schema_name, schema, deadline)


async def test_failed_lane_call_makes_the_run_incomplete(tmp_path, sessionmaker):
    case = tiny_case(tmp_path)
    recording = scripted(FlakyGroq())

    result = await run_one(sessionmaker, recording, case, "pipeline", [])

    assert result["status"] == "incomplete"
    # The job posts no partial review: the run fails as transient (Phase 6c fix 3).
    assert result["problems"] == ["run failed: 1 lane call(s) failed after their retries"]
    [failed] = [c for c in result["calls"] if c["error"]]
    assert failed["kind"] == "security" and "500" in failed["error"]


async def test_a_new_agent_runs_alone_on_another_evals_candidates(tmp_path, sessionmaker, groq):
    case, baseline, v2 = tiny_case(tmp_path), tmp_path / "baseline", tmp_path / "agent-v2"
    recording = scripted(groq)
    await run_session([case], ["pipeline"], sessionmaker, recording, baseline, 0)
    lane_calls = len(groq.calls)

    assert copy_pipeline_results([case], baseline, v2) == []
    await run_session([case], ["agent"], sessionmaker, recording, v2, 0)

    assert len(groq.calls) == lane_calls  # every lane call came from the cache
    copied = read_result(v2 / "c.pipeline.json")
    assert copied == read_result(baseline / "c.pipeline.json")
    agent = read_result(v2 / "c.agent.json")
    assert agent["status"] == "complete"
    assert agent["agent_prompt_version"] == AGENT_PROMPT_VERSION
    assert {c["kind"] for c in agent["calls"]} == {"agent"}


async def test_only_complete_current_pipeline_results_are_copied(tmp_path, sessionmaker, groq):
    case, baseline, v2 = tiny_case(tmp_path), tmp_path / "baseline", tmp_path / "agent-v2"
    await run_session([case], ["pipeline"], sessionmaker, scripted(groq), baseline, 0)
    source = read_result(baseline / "c.pipeline.json")

    # Stale (the case changed since) or incomplete: not copied, and reported.
    for broken in (source | {"head_sha": "0" * 40}, source | {"status": "incomplete"}):
        write_result(baseline / "c.pipeline.json", broken)
        assert copy_pipeline_results([case], baseline, v2) == ["c"]
        assert not (v2 / "c.pipeline.json").exists()

    # A done result already there is never overwritten.
    done = source | {"wall_seconds": 99.0}
    write_result(v2 / "c.pipeline.json", done)
    write_result(baseline / "c.pipeline.json", source)
    assert copy_pipeline_results([case], baseline, v2) == []
    assert read_result(v2 / "c.pipeline.json") == done


async def test_an_eval_run_can_pick_an_older_agent_prompt(tmp_path, sessionmaker, groq):
    case = tiny_case(tmp_path)

    result = await run_one(sessionmaker, scripted(groq), case, "agent", [], agent_prompt="v1")

    [system] = {call["messages"][0]["content"] for call in groq.agent_calls}
    assert system == (loop.PROMPTS_DIR / "v1.md").read_text(encoding="utf-8")
    assert (result["agent_prompt_version"], result["agent_prompt_override"]) == ("v1", True)
    assert loop.AGENT_PROMPT_VERSION == AGENT_PROMPT_VERSION  # prod's version is back

    default = await run_one(sessionmaker, scripted(groq), case, "pipeline", [])
    assert (default["agent_prompt_version"], default["agent_prompt_override"]) == (None, False)

    # Naming prod's own version isn't an override: nothing older ran.
    named = await run_one(
        sessionmaker, scripted(groq), case, "agent", [], agent_prompt=AGENT_PROMPT_VERSION
    )
    assert (named["agent_prompt_version"], named["agent_prompt_override"]) == (
        AGENT_PROMPT_VERSION,
        False,
    )
