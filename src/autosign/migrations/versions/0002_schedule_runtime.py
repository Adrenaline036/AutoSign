"""Add schedule retry and runtime state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_schedule_runtime"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.add_column(
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="2")
        )
        batch.add_column(
            sa.Column("retry_delay_seconds", sa.Integer(), nullable=False, server_default="300")
        )
        batch.add_column(sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_status", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("last_status")
        batch.drop_column("last_run_at")
        batch.drop_column("next_run_at")
        batch.drop_column("retry_delay_seconds")
        batch.drop_column("max_retries")
