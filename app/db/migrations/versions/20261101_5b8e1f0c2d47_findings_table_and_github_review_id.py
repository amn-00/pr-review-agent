"""findings table and github_review_id

Revision ID: 5b8e1f0c2d47
Revises: c25212634e60
Create Date: 2026-11-01 00:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b8e1f0c2d47"
down_revision: str | Sequence[str] | None = "c25212634e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Phase 2 posts a review instead of a comment. Renamed (not dropped and re-added) so
    # existing runs keep their IDs; autogenerate would have dropped the column.
    op.alter_column("review_runs", "github_comment_id", new_column_name="github_review_id")

    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("lane", sa.String(length=20), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("file", sa.Text(), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(length=10), nullable=True),
        sa.Column("posted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("drop_reason", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["review_runs.id"], name=op.f("fk_findings_run_id_review_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
    )
    op.create_index(op.f("ix_findings_run_id"), "findings", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_findings_run_id"), table_name="findings")
    op.drop_table("findings")
    op.alter_column("review_runs", "github_review_id", new_column_name="github_comment_id")
