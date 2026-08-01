"""Create accounts, encrypted secrets, schedules and execution records."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plugin_id", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_plugin_id", "accounts", ["plugin_id"])
    op.create_table(
        "app_metadata",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "account_secrets",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id", "name"),
    )
    op.create_table(
        "execution_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_execution_records_account_id", "execution_records", ["account_id"])
    op.create_index("ix_execution_records_status", "execution_records", ["status"])
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column("jitter_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedules_account_id", "schedules", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_schedules_account_id", table_name="schedules")
    op.drop_table("schedules")
    op.drop_index("ix_execution_records_status", table_name="execution_records")
    op.drop_index("ix_execution_records_account_id", table_name="execution_records")
    op.drop_table("execution_records")
    op.drop_table("account_secrets")
    op.drop_table("app_metadata")
    op.drop_index("ix_accounts_plugin_id", table_name="accounts")
    op.drop_table("accounts")
