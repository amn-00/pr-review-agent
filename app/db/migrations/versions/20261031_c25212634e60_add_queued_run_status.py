"""add queued run status

Revision ID: c25212634e60
Revises: a3eddede8e78
Create Date: 2026-10-31 23:21:06.040407

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c25212634e60"
down_revision: str | Sequence[str] | None = "a3eddede8e78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_review_runs_valid_status"


def upgrade() -> None:
    # Autogenerate doesn't detect CHECK constraint changes, so this one is written by hand.
    op.drop_constraint(op.f(CONSTRAINT), "review_runs", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "review_runs",
        "status IN ('queued', 'running', 'completed', 'failed', 'skipped')",
    )


def downgrade() -> None:
    # The old constraint has no 'queued'. Mark those runs failed: still retryable, and honest.
    op.execute("UPDATE review_runs SET status = 'failed' WHERE status = 'queued'")
    op.drop_constraint(op.f(CONSTRAINT), "review_runs", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "review_runs",
        "status IN ('running', 'completed', 'failed', 'skipped')",
    )
