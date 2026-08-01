from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plugin_id: Mapped[str] = mapped_column(String(100), index=True)
    label: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    secrets: Mapped[list[AccountSecret]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    schedules: Mapped[list[Schedule]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executions: Mapped[list[ExecutionRecord]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    notification_channel_links: Mapped[list[AccountNotificationChannel]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AccountSecret(Base):
    __tablename__ = "account_secrets"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    account: Mapped[Account] = relationship(back_populates="secrets")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100))
    cron_expression: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    jitter_seconds: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    account: Mapped[Account] = relationship(back_populates="schedules")


class ExecutionRecord(Base):
    __tablename__ = "execution_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(50), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    account: Mapped[Account] = relationship(back_populates="executions")


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    channel_type: Mapped[str] = mapped_column(String(50), index=True)
    encrypted_config: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    account_links: Mapped[list[AccountNotificationChannel]] = relationship(
        back_populates="channel",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AccountNotificationChannel(Base):
    __tablename__ = "account_notification_channels"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    channel_id: Mapped[str] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    account: Mapped[Account] = relationship(back_populates="notification_channel_links")
    channel: Mapped[NotificationChannel] = relationship(back_populates="account_links")


class AppMetadata(Base):
    __tablename__ = "app_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
