from __future__ import annotations

from fastapi import APIRouter, HTTPException

from autosign.core.backup import BackupError
from autosign.core.services.backups import BackupService
from autosign.web.schemas import BackupActionRead, BackupSettingsWrite, BackupStatusRead
from autosign.web.serialization import aware_utc


def create_backups_router(*, backups: BackupService) -> APIRouter:
    router = APIRouter()

    def serialize_status() -> BackupStatusRead:
        status = backups.status()
        return BackupStatusRead(
            enabled=status.enabled,
            configured=status.configured,
            daily_time=status.daily_time,
            timezone=status.timezone,
            retention_count=status.retention_count,
            next_run_at=aware_utc(status.next_run_at),
            last_attempt_at=aware_utc(status.last_attempt_at),
            last_success_at=aware_utc(status.last_success_at),
            last_failure_at=aware_utc(status.last_failure_at),
            last_error=status.last_error,
            latest_backup_name=status.latest_backup_name,
            latest_backup_size=status.latest_backup_size,
        )

    @router.get("/api/v1/backups/status", response_model=BackupStatusRead)
    async def backup_status() -> BackupStatusRead:
        return serialize_status()

    @router.post("/api/v1/backups/run", response_model=BackupActionRead)
    async def run_backup() -> BackupActionRead:
        try:
            destination = await backups.create_now()
        except BackupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return BackupActionRead(
            success=True,
            message=f"Encrypted backup created: {destination.name}",
            status=serialize_status(),
        )

    @router.post("/api/v1/backups/check-latest", response_model=BackupActionRead)
    async def check_latest_backup() -> BackupActionRead:
        try:
            destination = await backups.check_latest()
        except BackupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return BackupActionRead(
            success=True,
            message=f"Backup is valid: {destination.name}",
            status=serialize_status(),
        )

    @router.put("/api/v1/backups/settings", response_model=BackupStatusRead)
    async def update_backup_settings(request: BackupSettingsWrite) -> BackupStatusRead:
        try:
            await backups.update_settings(
                enabled=request.enabled,
                daily_time=request.daily_time,
                timezone=request.timezone,
                retention_count=request.retention_count,
                password=(
                    request.password.get_secret_value()
                    if request.password is not None
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return serialize_status()

    return router
