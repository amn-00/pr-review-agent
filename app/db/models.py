from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    # The comment we posted for this run, so a retry can tell it already posted.
    github_comment_id: Mapped[int | None] = mapped_column(BigInteger)
