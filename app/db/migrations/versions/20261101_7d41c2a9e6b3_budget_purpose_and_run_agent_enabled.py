"""budget purpose and run agent_enabled

Revision ID: 7d41c2a9e6b3
Revises: c09357351001
Create Date: 2026-11-01 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d41c2a9e6b3"
down_revision: str | Sequence[str] | None = "c09357351001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand: autogenerate doesn't detect primary key changes. Existing rows were all
    # spent by the lanes, so they become "pipeline"; the default is dropped afterwards so every
    # new row must say whose tokens it counts.
    op.add_column(
        "daily_token_usage",
        sa.Column("purpose", sa.String(length=10), nullable=False, server_default="pipeline"),
    )
    op.alter_column("daily_token_usage", "purpose", server_default=None)
    op.drop_constraint(op.f("pk_daily_token_usage"), "daily_token_usage", type_="primary")
    op.create_primary_key(
        op.f("pk_daily_token_usage"), "daily_token_usage", ["day", "model", "purpose"]
    )

    # Runs before Phase 4b had no agent.
    op.add_column(
        "review_runs",
        sa.Column("agent_enabled", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("review_runs", "agent_enabled")

    # Fold the agent's tokens into the pipeline's row for the same day and model, so the old
    # one-row-per-(day, model) key holds again and no spend is forgotten.
    op.execute(
        """
        UPDATE daily_token_usage AS p SET tokens = p.tokens + a.tokens
        FROM daily_token_usage AS a
        WHERE p.purpose = 'pipeline' AND a.purpose = 'agent'
          AND p.day = a.day AND p.model = a.model
        """
    )
    op.execute(
        """
        UPDATE daily_token_usage AS a SET purpose = 'pipeline'
        WHERE a.purpose = 'agent' AND NOT EXISTS (
            SELECT 1 FROM daily_token_usage AS p
            WHERE p.purpose = 'pipeline' AND p.day = a.day AND p.model = a.model
        )
        """
    )
    op.execute("DELETE FROM daily_token_usage WHERE purpose = 'agent'")
    op.drop_constraint(op.f("pk_daily_token_usage"), "daily_token_usage", type_="primary")
    op.create_primary_key(op.f("pk_daily_token_usage"), "daily_token_usage", ["day", "model"])
    op.drop_column("daily_token_usage", "purpose")
