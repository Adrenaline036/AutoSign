from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from autosign.core.backup import BackupError, create_backup, inspect_backup
from autosign.core.db import AppMetadata, Database
from autosign.core.security import SecretCipher, SecretDecryptionError

AUTO_BACKUP_GLOB = "autosign-auto-*.asbackup"
LAST_ATTEMPT_AT = "backup_last_attempt_at"
LAST_SUCCESS_AT = "backup_last_success_at"
LAST_ERROR = "backup_last_error"
LAST_FAILURE_AT = "backup_last_failure_at"
LAST_SCHEDULED_DATE = "backup_last_scheduled_date"
BACKUP_CONFIG = "backup_config"
BACKUP_PASSWORD = "backup_password"
BACKUP_PASSWORD_AAD = "autosign:backup-password"


@dataclass(frozen=True, slots=True)
class BackupStatusView:
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


class BackupService:
    def __init__(
        self,
        database: Database,
        *,
        database_path: Path,
        backup_dir: Path,
        master_key: str,
        cipher: SecretCipher,
        password: str | None,
        autosign_version: str,
        enabled: bool = False,
        daily_time: str = "03:30",
        timezone: str = "Asia/Shanghai",
        retention_count: int = 7,
    ) -> None:
        self._database = database
        self._database_path = database_path
        self._backup_dir = backup_dir
        self._master_key = master_key
        self._cipher = cipher
        self._password = password or None
        self._autosign_version = autosign_version
        self._enabled = enabled
        self._daily_time = daily_time
        self._timezone_name = timezone
        self._retention_count = retention_count
        self._run_lock = asyncio.Lock()
        self._parse_daily_time(daily_time)
        self._timezone(timezone)
        if retention_count < 1 or retention_count > 365:
            raise ValueError("Backup retention count must be between 1 and 365.")

    def initialize(self) -> None:
        raw_config = self._get_metadata(BACKUP_CONFIG)
        if raw_config is not None:
            try:
                config = json.loads(raw_config)
                enabled = bool(config["enabled"])
                daily_time = str(config["daily_time"])
                timezone = str(config["timezone"])
                retention_count = int(config["retention_count"])
                self._validate_settings(daily_time, timezone, retention_count)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._set_metadata(LAST_ERROR, f"Stored backup settings are invalid: {exc}")
                self._set_metadata(LAST_FAILURE_AT, datetime.now(UTC).isoformat())
            else:
                self._enabled = enabled
                self._daily_time = daily_time
                self._timezone_name = timezone
                self._retention_count = retention_count
        encrypted_password = self._get_metadata(BACKUP_PASSWORD)
        if encrypted_password is not None:
            try:
                self._password = self._cipher.decrypt(
                    encrypted_password,
                    associated_data=BACKUP_PASSWORD_AAD,
                )
            except SecretDecryptionError as exc:
                self._password = None
                self._set_metadata(LAST_ERROR, f"Stored backup password cannot be opened: {exc}")
                self._set_metadata(LAST_FAILURE_AT, datetime.now(UTC).isoformat())

    async def update_settings(
        self,
        *,
        enabled: bool,
        daily_time: str,
        timezone: str,
        retention_count: int,
        password: str | None = None,
    ) -> BackupStatusView:
        self._validate_settings(daily_time, timezone, retention_count)
        if password is not None and len(password) < 12:
            raise ValueError("Backup password must contain at least 12 characters.")
        async with self._run_lock:
            effective_password = password or self._password
            if enabled and effective_password is None:
                raise ValueError("Set a backup password before enabling automatic backup.")
            config = {
                "enabled": enabled,
                "daily_time": daily_time,
                "timezone": timezone,
                "retention_count": retention_count,
            }
            self._set_metadata(BACKUP_CONFIG, json.dumps(config, ensure_ascii=False))
            if password is not None:
                encrypted = self._cipher.encrypt(
                    password,
                    associated_data=BACKUP_PASSWORD_AAD,
                )
                self._set_metadata(BACKUP_PASSWORD, encrypted)
                self._password = password
            self._enabled = enabled
            self._daily_time = daily_time
            self._timezone_name = timezone
            self._retention_count = retention_count
            self._delete_metadata(LAST_SCHEDULED_DATE)
        return self.status()

    async def create_now(self) -> Path:
        if self._password is None:
            raise BackupError("Automatic backup password is not configured.")
        async with self._run_lock:
            attempted_at = datetime.now(UTC)
            self._set_metadata(LAST_ATTEMPT_AT, attempted_at.isoformat())
            try:
                destination = await asyncio.to_thread(
                    create_backup,
                    database_path=self._database_path,
                    master_key=self._master_key,
                    password=self._password,
                    output_dir=self._backup_dir,
                    autosign_version=self._autosign_version,
                    filename_prefix="autosign-auto",
                )
                await asyncio.to_thread(inspect_backup, destination, self._password)
                await asyncio.to_thread(self._prune)
            except Exception as exc:
                self._set_metadata(LAST_ERROR, str(exc)[:1000])
                self._set_metadata(LAST_FAILURE_AT, datetime.now(UTC).isoformat())
                raise
            self._set_metadata(LAST_SUCCESS_AT, datetime.now(UTC).isoformat())
            return destination

    async def check_latest(self) -> Path:
        if self._password is None:
            raise BackupError("Automatic backup password is not configured.")
        latest = self._latest_backup()
        if latest is None:
            raise BackupError("No automatic backup is available yet.")
        await asyncio.to_thread(inspect_backup, latest, self._password)
        return latest

    async def run_if_due(self, now: datetime | None = None) -> Path | None:
        if not self._enabled or self._password is None:
            return None
        now = self._aware_utc(now or datetime.now(UTC))
        local_now = now.astimezone(self._timezone(self._timezone_name))
        hour, minute = self._parse_daily_time(self._daily_time)
        scheduled = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_now < scheduled:
            return None
        today = local_now.date().isoformat()
        if self._get_metadata(LAST_SCHEDULED_DATE) == today:
            return None
        self._set_metadata(LAST_SCHEDULED_DATE, today)
        return await self.create_now()

    def status(self, now: datetime | None = None) -> BackupStatusView:
        now = self._aware_utc(now or datetime.now(UTC))
        latest = self._latest_backup()
        return BackupStatusView(
            enabled=self._enabled,
            configured=self._password is not None,
            daily_time=self._daily_time,
            timezone=self._timezone_name,
            retention_count=self._retention_count,
            next_run_at=self._next_run(now) if self._enabled else None,
            last_attempt_at=self._metadata_datetime(LAST_ATTEMPT_AT),
            last_success_at=self._metadata_datetime(LAST_SUCCESS_AT),
            last_failure_at=self._metadata_datetime(LAST_FAILURE_AT),
            last_error=self._get_metadata(LAST_ERROR),
            latest_backup_name=latest.name if latest else None,
            latest_backup_size=latest.stat().st_size if latest else None,
        )

    def _next_run(self, now: datetime) -> datetime:
        zone = self._timezone(self._timezone_name)
        local_now = now.astimezone(zone)
        hour, minute = self._parse_daily_time(self._daily_time)
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        attempted_today = self._get_metadata(LAST_SCHEDULED_DATE) == local_now.date().isoformat()
        if candidate <= local_now or attempted_today:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def _latest_backup(self) -> Path | None:
        if not self._backup_dir.is_dir():
            return None
        files = sorted(self._backup_dir.glob(AUTO_BACKUP_GLOB), key=lambda path: path.name)
        return files[-1] if files else None

    def _prune(self) -> None:
        files = sorted(self._backup_dir.glob(AUTO_BACKUP_GLOB), key=lambda path: path.name)
        for path in files[:-self._retention_count]:
            if path.is_file() and path.parent.resolve() == self._backup_dir.resolve():
                path.unlink()

    def _get_metadata(self, key: str) -> str | None:
        with self._database.session() as session:
            record = session.get(AppMetadata, key)
            return record.value if record is not None else None

    def _set_metadata(self, key: str, value: str) -> None:
        with self._database.session() as session:
            record = session.get(AppMetadata, key)
            if record is None:
                session.add(AppMetadata(key=key, value=value))
            else:
                record.value = value
            session.commit()

    def _delete_metadata(self, key: str) -> None:
        with self._database.session() as session:
            record = session.get(AppMetadata, key)
            if record is not None:
                session.delete(record)
                session.commit()

    def _metadata_datetime(self, key: str) -> datetime | None:
        value = self._get_metadata(key)
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _parse_daily_time(value: str) -> tuple[int, int]:
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Backup daily time must use HH:MM.") from exc
        if parsed.second or parsed.microsecond:
            raise ValueError("Backup daily time must use HH:MM.")
        return parsed.hour, parsed.minute

    @classmethod
    def _validate_settings(
        cls,
        daily_time: str,
        timezone: str,
        retention_count: int,
    ) -> None:
        cls._parse_daily_time(daily_time)
        cls._timezone(timezone)
        if retention_count < 1 or retention_count > 365:
            raise ValueError("Backup retention count must be between 1 and 365.")

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown backup timezone: {name}") from exc

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BackupCoordinator:
    def __init__(self, backups: BackupService, *, poll_seconds: float = 60) -> None:
        self._backups = backups
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._logger = logging.getLogger("autosign.backups")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def poll_once(self, now: datetime | None = None) -> Path | None:
        return await self._backups.run_if_due(now)

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Automatic backup failed")
            await asyncio.sleep(self._poll_seconds)
