from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, SecretStr


class AccountCreate(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class AccountUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    settings: dict[str, Any] | None = None


class AccountRead(BaseModel):
    id: str
    plugin_id: str
    label: str
    enabled: bool
    settings: dict[str, Any]
    secret_names: list[str] = Field(default_factory=list)
    monitor_configured: bool = False
    napcat_configured: bool = False
    notification_channel_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class AccountDelete(BaseModel):
    confirm_label: str


class SecretWrite(BaseModel):
    value: SecretStr = Field(min_length=1)


class SecretList(BaseModel):
    names: list[str]


class BrowserSessionRead(BaseModel):
    id: str
    account_id: str
    url: str
    title: str
    created_at: datetime
    last_activity: datetime
    viewport_width: int
    viewport_height: int


class BrowserClick(BaseModel):
    x: float
    y: float


class BrowserTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class BrowserKeyInput(BaseModel):
    key: str


class BrowserSessionClose(BaseModel):
    save_state: bool = False
    force_save: bool = False


class BrowserSessionCloseResult(BaseModel):
    saved: bool
    verified: bool
    secret_names: list[str]


class AdminPasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=12, max_length=200)


class AuthStatus(BaseModel):
    configured: bool
    authenticated: bool
    csrf_token: str | None = None


class ExecutionRead(BaseModel):
    id: str
    account_id: str
    account_label: str
    plugin_id: str
    status: str
    message: str
    verified: bool
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    details: dict[str, Any] = Field(default_factory=dict)


class ScheduleWrite(BaseModel):
    enabled: bool = True
    daily_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Asia/Shanghai"
    jitter_minutes: int = Field(default=15, ge=0, le=180)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_minutes: int = Field(default=5, ge=1, le=120)


class ScheduleRead(BaseModel):
    id: str
    account_id: str
    account_label: str
    enabled: bool
    daily_time: str
    timezone: str
    jitter_minutes: int
    max_retries: int
    retry_delay_minutes: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None


class MonitorPushWrite(BaseModel):
    push_url: SecretStr = Field(min_length=10, max_length=2048)


class MonitorDeliveryRead(BaseModel):
    configured: bool
    success: bool
    message: str


class NapCatConfigWrite(BaseModel):
    base_url: str = Field(min_length=8, max_length=2048)
    access_token: SecretStr = Field(min_length=1, max_length=512)
    target_type: str = Field(pattern=r"^(private|group)$")
    target_id: str = Field(pattern=r"^\d{5,20}$")


class NotificationChannelWrite(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    channel_type: str = Field(pattern=r"^(uptime_kuma|napcat)$")
    push_url: SecretStr | None = Field(default=None, min_length=10, max_length=2048)
    base_url: str | None = Field(default=None, min_length=8, max_length=2048)
    access_token: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    target_type: str | None = Field(default=None, pattern=r"^(private|group)$")
    target_id: str | None = Field(default=None, pattern=r"^\d{5,20}$")


class NotificationChannelRead(BaseModel):
    id: str
    name: str
    channel_type: str
    assigned_account_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class NotificationChannelAssignmentWrite(BaseModel):
    channel_ids: list[str] = Field(default_factory=list, max_length=100)


class NotificationChannelDeliveryRead(BaseModel):
    channel_id: str
    channel_name: str
    channel_type: str
    success: bool
    message: str


class BackupStatusRead(BaseModel):
    enabled: bool
    configured: bool
    daily_time: str
    timezone: str
    retention_count: int
    next_run_at: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    latest_backup_name: str | None
    latest_backup_size: int | None


class BackupActionRead(BaseModel):
    success: bool
    message: str
    status: BackupStatusRead


class BackupSettingsWrite(BaseModel):
    enabled: bool
    daily_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=100)
    retention_count: int = Field(ge=1, le=365)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=200)
