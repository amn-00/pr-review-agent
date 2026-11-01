from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names, so migrations can refer to them by name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    # Every column of the constraint, e.g. uq_review_runs_pr_id_head_sha
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class RunStatus(StrEnum):
    # Not claimed by a worker: just created, or released to wait for a retry.
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # The PR moved on (newer head SHA, closed, or converted to draft) before we reviewed it.
    SKIPPED = "skipped"


class BudgetPurpose(StrEnum):
    """Who spent the tokens. Each model's daily budget is split between the two."""

    PIPELINE = "pipeline"  # the lanes
    AGENT = "agent"  # the verification agent


class Installation(Base):
    __tablename__ = "installations"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    account: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("installations.id"))
    # GitHub's repo ID is stable; full_name changes when a repo is renamed or transferred.
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(255))


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(255))


class ReviewRun(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        # The durable dedupe: at most one run per commit of a PR, even if Redis loses its data.
        UniqueConstraint("pr_id", "head_sha"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'skipped')",
            name="valid_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pr_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id"))
    head_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.QUEUED)
    # When the current attempt claimed the run. A `running` run older than the job timeout
    # belongs to a worker that died, and can be taken over.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 9 decimal places: one lane call costs ~$0.00003, and calls are added one at a time, so
    # 6 places would round every addition by up to a few percent.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 9), default=0, server_default="0")
    # Totals over all attempts of the run, added to after every lane call: llm_calls vs.
    # cached_calls, and tokens_saved = what cache hits on other runs' answers originally cost.
    # "The cache saved X% of tokens" = tokens_saved / (tokens_saved + tokens_in + tokens_out).
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cached_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tokens_saved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    # Whether the verification agent checked this run's findings (AGENT_ENABLED at the time),
    # so evals can tell "pipeline only" runs from "pipeline + agent" runs.
    agent_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The review we posted for this run.
    github_review_id: Mapped[int | None] = mapped_column(BigInteger)


class Finding(Base):
    """Every candidate a lane produced for a run, posted or not. Dropped ones are kept (with
    drop_reason) so evals can measure how often the hallucination guard and dedupe fire."""

    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Indexed: "all findings of a run" is the main lookup.
    run_id: Mapped[int] = mapped_column(ForeignKey("review_runs.id"), index=True)
    lane: Mapped[str] = mapped_column(String(20))
    prompt_version: Mapped[str] = mapped_column(String(20))
    file: Mapped[str] = mapped_column(Text)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(20))
    severity: Mapped[str] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    # Set by the verification agent: confirmed | dropped | uncertain. NULL = not verified.
    verdict: Mapped[str | None] = mapped_column(String(10))
    # The agent's reason, or why it didn't run (e.g. "time limit").
    verdict_reason: Mapped[str | None] = mapped_column(Text)
    # [{path, line, why}]: lines the agent read that support the verdict.
    evidence: Mapped[list | None] = mapped_column(JSONB)
    posted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    drop_reason: Mapped[str | None] = mapped_column(String(20))  # not_in_diff | duplicate
    # For a duplicate: the finding that beat it in aggregate (same run). Lets evals tell which
    # duplicates were lost because their winner was dropped by the agent.
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("findings.id"))


class AgentStep(Base):
    """One step of the verification agent: an LLM call and the tool it called."""

    __tablename__ = "agent_steps"
    # Also the index for "all steps of a finding".
    __table_args__ = (UniqueConstraint("finding_id", "step_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Steps go with their finding: a retry replaces the run's findings, and their steps.
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"))
    step_no: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str | None] = mapped_column(String(30))  # NULL: the call failed or called none
    args_json: Mapped[dict | None] = mapped_column(JSONB)
    result_chars: Mapped[int] = mapped_column(Integer)
    result_summary: Mapped[str] = mapped_column(Text)  # the first 300 characters
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)


class ChunkCache(Base):
    """One lane's answer for one chunk, keyed by app.pipeline.cache.cache_key."""

    __tablename__ = "chunk_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    # The model's findings, with line numbers relative to the chunk's first line.
    findings: Mapped[list] = mapped_column(JSONB)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    # The run that paid for this answer. Hits from other runs count as tokens saved; a retry
    # reusing its own run's calls doesn't. No foreign key: the cache outlives runs.
    run_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyTokenUsage(Base):
    """Tokens spent per model, purpose and UTC day, across all PRs, for the daily budgets."""

    __tablename__ = "daily_token_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(10), primary_key=True)  # a BudgetPurpose
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)
