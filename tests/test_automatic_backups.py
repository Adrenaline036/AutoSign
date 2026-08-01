from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from autosign.core import backup
from autosign.core.backup import BackupError
from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.backups import BackupService
from autosign.core.services.vault import VaultService

PASSWORD = "automatic backup password"


@pytest.fixture(autouse=True)
def faster_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "KDF_N", 2**10)


@pytest.fixture
def backup_service_data(tmp_path: Path) -> tuple[Database, Path, str]:
    database_path = tmp_path / "autosign.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.migrate()
    master_key = SecretCipher.generate_key()
    VaultService(database, SecretCipher(master_key)).initialize_key_check()
    yield database, database_path, master_key
    database.dispose()


def make_service(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
    *,
    password: str | None = PASSWORD,
    enabled: bool = True,
    retention_count: int = 2,
) -> BackupService:
    database, database_path, master_key = backup_service_data
    return BackupService(
        database,
        database_path=database_path,
        backup_dir=tmp_path / "backups",
        master_key=master_key,
        cipher=SecretCipher(master_key),
        password=password,
        autosign_version="test-version",
        enabled=enabled,
        daily_time="03:30",
        timezone="Asia/Shanghai",
        retention_count=retention_count,
    )


@pytest.mark.asyncio
async def test_manual_automatic_backup_is_created_and_verified(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
) -> None:
    service = make_service(tmp_path, backup_service_data, enabled=False)

    destination = await service.create_now()
    checked = await service.check_latest()
    status = service.status()

    assert destination == checked
    assert destination.name.startswith("autosign-auto-")
    assert status.configured is True
    assert status.enabled is False
    assert status.last_success_at is not None
    assert status.last_error is None
    assert status.latest_backup_name == destination.name


@pytest.mark.asyncio
async def test_scheduled_backup_runs_only_once_per_local_day(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
) -> None:
    service = make_service(tmp_path, backup_service_data)
    after_schedule = datetime(2026, 8, 1, 4, 0, tzinfo=UTC)

    first = await service.run_if_due(after_schedule)
    second = await service.run_if_due(after_schedule)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_retention_only_removes_old_automatic_backups(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
) -> None:
    service = make_service(tmp_path, backup_service_data, enabled=False, retention_count=2)
    backup_dir = tmp_path / "backups"
    manual = backup_dir / "autosign-manual-kept.asbackup"
    backup_dir.mkdir()
    manual.write_bytes(b"manual")

    created = [await service.create_now() for _ in range(3)]

    remaining = sorted(backup_dir.glob("autosign-auto-*.asbackup"))
    assert len(remaining) == 2
    assert created[0] not in remaining
    assert manual.read_bytes() == b"manual"


@pytest.mark.asyncio
async def test_missing_password_disables_backup_actions(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
) -> None:
    service = make_service(tmp_path, backup_service_data, password=None)

    assert service.status().configured is False
    assert await service.run_if_due(datetime.now(UTC)) is None
    with pytest.raises(BackupError, match="password is not configured"):
        await service.create_now()


@pytest.mark.asyncio
async def test_gui_settings_are_encrypted_and_survive_restart(
    tmp_path: Path,
    backup_service_data: tuple[Database, Path, str],
) -> None:
    service = make_service(tmp_path, backup_service_data, password=None, enabled=False)
    await service.update_settings(
        enabled=True,
        daily_time="04:45",
        timezone="UTC",
        retention_count=11,
        password=PASSWORD,
    )
    database, database_path, master_key = backup_service_data
    database_contents = database_path.read_bytes().decode("utf-8", errors="ignore")
    assert PASSWORD not in database_contents

    restarted = BackupService(
        database,
        database_path=database_path,
        backup_dir=tmp_path / "backups",
        master_key=master_key,
        cipher=SecretCipher(master_key),
        password=None,
        autosign_version="test-version",
    )
    restarted.initialize()
    status = restarted.status()

    assert status.enabled is True
    assert status.configured is True
    assert status.daily_time == "04:45"
    assert status.timezone == "UTC"
    assert status.retention_count == 11
