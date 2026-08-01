"""Add reusable notification channels and account assignments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_notification_channels"
down_revision = "0002_schedule_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("channel_type", sa.String(length=50), nullable=False),
        sa.Column("encrypted_config", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_channels_channel_type",
        "notification_channels",
        ["channel_type"],
    )
    op.create_table(
        "account_notification_channels",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("channel_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", "channel_id"),
    )


def downgrade() -> None:
    op.drop_table("account_notification_channels")
    op.drop_index(
        "ix_notification_channels_channel_type",
        table_name="notification_channels",
    )
    op.drop_table("notification_channels")
