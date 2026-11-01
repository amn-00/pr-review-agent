"""Run the eval cases through the real review job: real Groq, a fake GitHub, its own database.

    uv run python -m evals.run --name baseline [--only CASE ...] [--mode MODE ...] [--dry-run]

Each case runs in up to three modes, in this order:
- pipeline: AGENT_ENABLED off, PR #1 at the case's head. Pays for the lanes.
- agent:    AGENT_ENABLED on, PR #2 at the same head. Every lane call comes from the cache
            (same repo, same chunks), so it pays only for the agent, and both modes judge
            exactly the same candidates.
- repush:   pipeline mode, PR #1 at the push2 commit, for the cache's tokens-saved metric.

Results go to evals/results/<name>/<case>.<mode>.json, one file per run. It's resumable: a
complete result for the current case files is never run again; an incomplete one (cut short
by the daily token budget, or a failed call) runs again from scratch, and the lane calls it
already paid for come from the cache. Runs that don't fit today's budget are left for
another day.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
from alembic import command
from alembic.config import Config
from arq import Retry
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.loop import FINDING_TOKEN_LIMIT
from app.agent.verify import (
    AGENT_DAILY_TOKEN_BUDGET,
    FINDING_ESTIMATE_TOKENS,
    NOT_VERIFIED_DAILY_BUDGET,
)
from app.config import get_settings
from app.db.models import AgentStep, BudgetPurpose, Finding, PullRequest, Repo, ReviewRun
from app.db.queries import daily_tokens_used, get_cached
from app.db.session import make_engine, make_sessionmaker
from app.llm.groq import GroqClient
from app.log import setup_logging
from app.pipeline.cache import cache_key
from app.pipeline.lanes import LANES, estimate_call_tokens, lane_models
from app.pipeline.orchestrator import (
    PIPELINE_DAILY_TOKEN_BUDGET,
    PR_TOKEN_BUDGET,
    SKIP_DAILY_BUDGET,
    utc_today,
)
from app.worker import load_chunks, review_pr
from evals.dataset import Case, load_cases, validate
from evals.fake_github import FakeGitHub

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "evals" / "results"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review_eval"
MODES = ("pipeline", "agent", "repush")
PR_NUMBERS = {"pipeline": 1, "agent": 2, "repush": 1}
INSTALLATION_ID = 1
# Each run starts with a fresh per-minute Groq window, so latencies are comparable.
COOLDOWN_SECONDS = 60
SEMGREP_VERSION = "1.180.0"

settings = get_settings()


class RecordingGroq:
    """Wraps the Groq client and records every call: lane or agent, model, tokens, latency,
    and how long it waited for rate limits. The run's DB totals don't split time."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    async def complete_json(self, model, messages, schema_name, schema, deadline):
        call = self.inner.complete_json(model, messages, schema_name, schema, deadline)
        return await self._record(call, model, schema_name.removesuffix("_findings"))

    async def complete_tools(self, model, messages, tools, tool_choice, deadline):
        call = self.inner.complete_tools(model, messages, tools, tool_choice, deadline)
        return await self._record(call, model, "agent")

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def _record(self, call, model: str, kind: str):
        record = {"kind": kind, "model": model, "tokens_in": 0, "tokens_out": 0}
        record |= {"waited_s": 0.0, "error": None}
        started = time.monotonic()
        try:
            result = await call
        except Exception as exc:
            record["error"] = repr(exc)[:300]
            raise
        else:
            record["tokens_in"], record["tokens_out"] = result.tokens_in, result.tokens_out
            record["waited_s"] = round(result.waited_seconds, 2)
            return result
        finally:
            record["latency_s"] = round(time.monotonic() - started, 2)
            self.calls.append(record)


def pr_event(case: Case, number: int, sha: str) -> dict:
    """The trimmed webhook payload the job takes."""
    return {
        "action": "synchronize",
        "installation": {"id": INSTALLATION_ID},
        "repository": {
            "id": case.repo_id,
            "full_name": f"evals/{case.id}",
            "owner": {"login": "evals"},
        },
        "pull_request": {
            "number": number,
            "title": case.title,
            "head": {"sha": sha},
            "user": {"login": "eval-author"},
        },
    }


def result_path(results_dir: Path, case: Case, mode: str) -> Path:
    return results_dir / f"{case.id}.{mode}.json"


def read_result(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)  # never a half-written result


def is_done(result: dict | None, sha: str) -> bool:
    return result is not None and result["status"] == "complete" and result["head_sha"] == sha


async def lane_estimate(sessionmaker: async_sessionmaker, case: Case, chunks) -> dict[str, int]:
    """Tokens per model the lanes will spend: estimates for every call not in the cache."""
    models = lane_models(settings.groq_model, settings.groq_small_model)
    needed = dict.fromkeys(set(models.values()), 0)
    async with sessionmaker() as session:
        for chunk in chunks:
            for lane in LANES:
                key = cache_key(case.repo_id, lane, models[lane], chunk)
                if not await get_cached(session, key):
                    needed[models[lane]] += estimate_call_tokens(lane, chunk)
    return needed


async def over_budget(
    sessionmaker: async_sessionmaker, needed: dict[str, int], purpose: BudgetPurpose
) -> list[str]:
    """Models whose daily budget left can't cover what's needed, e.g. ["openai/gpt-oss-120b
    needs 14000, 9000 left"]. Empty when the run fits."""
    if purpose == BudgetPurpose.PIPELINE:
        budget = PIPELINE_DAILY_TOKEN_BUDGET
    else:
        budget = AGENT_DAILY_TOKEN_BUDGET
    problems = []
    async with sessionmaker() as session:
        for model, tokens in needed.items():
            left = budget - await daily_tokens_used(session, model, utc_today(), purpose)
            if tokens > left:
                problems.append(f"{model} needs {tokens}, {left} left")
    return problems


async def delete_run(sessionmaker: async_sessionmaker, case: Case, number: int, sha: str) -> None:
    """Remove an earlier attempt's run (and its findings and steps) so the job starts fresh.
    The lane cache isn't touched: what was paid for is reused."""
    async with sessionmaker() as session:
        run_id = await session.scalar(
            select(ReviewRun.id)
            .join(PullRequest, ReviewRun.pr_id == PullRequest.id)
            .join(Repo, PullRequest.repo_id == Repo.id)
            .where(
                Repo.github_repo_id == case.repo_id,
                PullRequest.number == number,
                ReviewRun.head_sha == sha,
            )
        )
        if run_id is not None:
            # One statement for all findings, so duplicate_of links between them don't block it.
            await session.execute(delete(Finding).where(Finding.run_id == run_id))
            await session.execute(delete(ReviewRun).where(ReviewRun.id == run_id))
            await session.commit()


async def run_one(
    sessionmaker: async_sessionmaker,
    groq: RecordingGroq,
    case: Case,
    mode: str,
    tool_findings: list[dict],
) -> dict:
    """Run the review job once and collect everything the report needs."""
    tree, sha = case.target(mode)
    number = PR_NUMBERS[mode]
    github = FakeGitHub(case.base, tree, sha, case.check_runs)
    groq.calls = []
    deferred = None
    agent_was_enabled = settings.agent_enabled
    settings.agent_enabled = mode == "agent"
    started_at = datetime.now(UTC)
    started = time.monotonic()
    try:
        ctx = {"sessionmaker": sessionmaker, "github": github, "groq": groq}
        await review_pr(ctx, pr_event(case, number, sha), f"eval:{case.id}:{mode}")
    except Retry as exc:
        # "Nothing reviewed under the daily budget": the job released the run to retry later.
        deferred = f"deferred by the job: {exc!r}"
    finally:
        settings.agent_enabled = agent_was_enabled
    wall_seconds = time.monotonic() - started

    run, findings = await _load_run(sessionmaker, case, number, sha)
    review = github.reviews[-1] if github.reviews else None
    problems, notes = _completeness(run, findings, review, deferred)
    return {
        "case": case.id,
        "mode": mode,
        "status": "incomplete" if problems else "complete",
        "problems": problems,
        "notes": notes,
        "head_sha": sha,
        "started_at": started_at.isoformat(),
        "wall_seconds": round(wall_seconds, 1),
        "run": run,
        "calls": groq.calls,
        "earlier_calls": [],
        "tool_findings": tool_findings,
        "findings": findings,
        "review": review,
    }


async def _load_run(
    sessionmaker: async_sessionmaker, case: Case, number: int, sha: str
) -> tuple[dict, list[dict]]:
    async with sessionmaker() as session:
        run = await session.scalar(
            select(ReviewRun)
            .join(PullRequest, ReviewRun.pr_id == PullRequest.id)
            .join(Repo, PullRequest.repo_id == Repo.id)
            .where(
                Repo.github_repo_id == case.repo_id,
                PullRequest.number == number,
                ReviewRun.head_sha == sha,
            )
        )
        rows = list(
            await session.scalars(
                select(Finding).where(Finding.run_id == run.id).order_by(Finding.id)
            )
        )
        steps = list(
            await session.scalars(
                select(AgentStep)
                .where(AgentStep.finding_id.in_([row.id for row in rows]))
                .order_by(AgentStep.finding_id, AgentStep.step_no)
            )
        )
    findings = []
    for row in rows:
        finding = _columns(row, skip={"run_id"})
        finding["agent_steps"] = [
            _columns(step, skip={"id", "finding_id"}) for step in steps if step.finding_id == row.id
        ]
        findings.append(finding)
    return _columns(run, skip={"pr_id", "github_review_id"}), findings


def _columns(row, skip: set[str]) -> dict:
    return {c.key: getattr(row, c.key) for c in row.__table__.columns if c.key not in skip}


def _completeness(
    run: dict, findings: list[dict], review: dict | None, deferred: str | None
) -> tuple[list[str], list[str]]:
    """Problems make a result incomplete (it runs again another day): the eval's quota ran
    out, or a call failed. Notes are the system's normal limits (time, per-PR budget), which
    are part of what's measured."""
    problems, notes = [], []
    if deferred:
        problems.append(deferred)
    if run["status"] != "completed":
        problems.append(f"run {run['status']}: {run['error']}")
    body = review["body"] if review else ""
    # The summary lists lane calls not run and failed calls (app/pipeline/review.py).
    for count, reason in re.findall(r"- (\d+) lane call\(s\) not run \(([^)]+)\)", body):
        message = f"{count} lane call(s) not run ({reason})"
        (problems if reason == SKIP_DAILY_BUDGET else notes).append(message)
    failed = re.search(r"- (\d+) lane call\(s\) failed", body)
    if failed:
        problems.append(f"{failed.group(1)} lane call(s) failed")
    for finding in findings:
        if finding["drop_reason"] or finding["verdict"] or not run["agent_enabled"]:
            continue
        message = f"finding {finding['id']} not verified ({finding['verdict_reason']})"
        reason = finding["verdict_reason"]
        (problems if reason == NOT_VERIFIED_DAILY_BUDGET else notes).append(message)
    return problems, notes


async def run_session(
    cases: list[Case],
    modes: list[str],
    sessionmaker: async_sessionmaker,
    groq: RecordingGroq,
    results_dir: Path,
    cooldown_seconds: float = COOLDOWN_SECONDS,
    dry_run: bool = False,
) -> None:
    for case in cases:
        for mode in modes:
            if mode == "repush" and case.push2 is None:
                continue
            tree, sha = case.target(mode)
            path = result_path(results_dir, case, mode)
            previous = read_result(path)
            label = f"{case.id} [{mode}]"
            if is_done(previous, sha):
                continue
            if mode != "pipeline" and not is_done(
                read_result(result_path(results_dir, case, "pipeline")), case.target("pipeline")[1]
            ):
                print(f"{label}: waiting for the pipeline run")
                continue

            github = FakeGitHub(case.base, tree, sha, case.check_runs)
            number = PR_NUMBERS[mode]
            chunks, _, _ = await load_chunks(github, INSTALLATION_ID, case.id, number, sha)
            if mode == "agent":
                pipeline = read_result(result_path(results_dir, case, "pipeline"))
                kept = sum(1 for f in pipeline["findings"] if not f["drop_reason"])
                # A margin of one finding at its cap, since a finding can spend more than
                # the average.
                tokens = kept * FINDING_ESTIMATE_TOKENS + FINDING_TOKEN_LIMIT if kept else 0
                needed = {settings.groq_model: tokens}
                purpose = BudgetPurpose.AGENT
            else:
                needed = await lane_estimate(sessionmaker, case, chunks)
                purpose = BudgetPurpose.PIPELINE
            estimate = ", ".join(f"{model} ~{tokens}" for model, tokens in needed.items())
            if dry_run:
                too_big = sum(needed.values()) > PR_TOKEN_BUDGET
                warning = " (over the per-PR budget!)" if too_big else ""
                print(f"{label}: {len(chunks)} chunk(s), {estimate}{warning}")
                continue
            problems = await over_budget(sessionmaker, needed, purpose)
            if problems:
                print(f"{label}: left for another day ({'; '.join(problems)})")
                continue

            earlier = []
            if previous is not None and previous["head_sha"] == sha:
                # An incomplete attempt: its calls were paid for, so they count in the cost.
                earlier = previous["earlier_calls"] + previous["calls"]
            await delete_run(sessionmaker, case, number, sha)
            print(f"{label}: running ({estimate})", flush=True)
            await asyncio.sleep(cooldown_seconds)
            tool_findings = [
                {"file": chunk.path, **asdict(hint)}
                for chunk in chunks
                for hints in chunk.hints.values()
                for hint in hints
            ]
            result = await run_one(sessionmaker, groq, case, mode, tool_findings)
            result["earlier_calls"] = earlier
            write_result(path, result)
            posted = sum(1 for f in result["findings"] if f["posted"])
            print(
                f"{label}: {result['status']} in {result['wall_seconds']} s, "
                f"{len(result['findings'])} candidate(s), {posted} posted"
                + (f" — {'; '.join(result['problems'])}" if result["problems"] else ""),
                flush=True,
            )


def prepare_database(url: str) -> None:
    """Create the eval database if needed and run the migrations on it."""
    asyncio.run(_create_database_if_missing(url))
    os.environ["DATABASE_URL"] = url  # read by the migrations' env.py
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


async def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
    admin = target.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target.database}"')
    finally:
        await conn.close()


async def _main(args: argparse.Namespace, cases: list[Case], url: str) -> None:
    engine = make_engine(url)
    groq = RecordingGroq(GroqClient(httpx.AsyncClient(), settings.groq_api_key.get_secret_value()))
    try:
        await run_session(
            cases,
            args.mode,
            make_sessionmaker(engine),
            groq,
            RESULTS_DIR / args.name,
            dry_run=args.dry_run,
        )
    finally:
        await groq.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="results folder under evals/results/")
    parser.add_argument("--only", nargs="+", metavar="CASE", help="run only these case IDs")
    parser.add_argument("--mode", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--dry-run", action="store_true", help="estimate tokens, call no LLM")
    args = parser.parse_args()

    # A missing Semgrep would silently remove every Semgrep hint (a tool problem never fails a
    # review), which would skew the results.
    if not shutil.which("semgrep"):
        sys.exit(f"Semgrep isn't on PATH. Install it: uv tool install semgrep=={SEMGREP_VERSION}")
    settings.semgrep_enabled = True

    cases = load_cases()
    if args.only:
        cases = [case for case in cases if case.id in args.only]
    invalid = {case.id: validate(case) for case in cases if validate(case)}
    if invalid:
        sys.exit(f"Invalid cases: {json.dumps(invalid, indent=1)}")

    setup_logging("WARNING")
    url = os.environ.get("EVAL_DATABASE_URL", DEFAULT_DATABASE_URL)
    prepare_database(url)
    asyncio.run(_main(args, cases, url))
    print(f"Done for now. Report: uv run python -m evals.report --name {args.name}")


if __name__ == "__main__":
    main()
