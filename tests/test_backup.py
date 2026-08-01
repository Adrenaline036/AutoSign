from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autosign.core import backup
from autosign.core.backup import BackupError, create_backup, inspect_backup, stage_restore
from autosign.core.db import Account, Database
from autosign.core.security import SecretCipher
from autosign.core.services.vault import VaultService

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def faster_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "KDF_N", 2**10)


@pytest.fixture
def initialized_data(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "autosign.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.migrate()
    key = SecretCipher.generate_key()
    vault = VaultService(database, SecretCipher(key))
    vault.initialize_key_check()
    with database.session() as session:
        account = Account(plugin_id="demo", label="Backup Test")
        session.add(account)
        session.commit()
        account_id = account.id
    vault.set(account_id, "browser_storage_state", '{"cookies":[]}')
    database.dispose()
    return database_path, key


def make_backup(tmp_path: Path, initialized_data: tuple[Path, str]) -> Path:
    database_path, key = initialized_data
    return create_backup(
        database_path=database_path,
        master_key=key,
        password=PASSWORD,
        output_dir=tmp_path / "backups",
        autosign_version="test-version",
    )


def test_encrypted_backup_round_trip(
    tmp_path: Path,
    initialized_data: tuple[Path, str],
) -> None:
    archive = make_backup(tmp_path, initialized_data)

    inspection = inspect_backup(archive, PASSWORD)

    assert archive.suffix == ".asbackup"
    assert archive.read_bytes().startswith(backup.BACKUP_MAGIC)
    assert b"AUTOSIGN_MASTER_KEY" not in archive.read_bytes()
    assert inspection.autosign_version == "test-version"
    assert inspection.schema_version == "0003_notification_channels"
    assert inspection.counts["accounts"] == 1
    assert inspection.counts["account_secrets"] == 1


def test_wrong_password_and_tampering_fail_safely(
    tmp_path: Path,
    initialized_data: tuple[Path, str],
) -> None:
    archive = make_backup(tmp_path, initialized_data)

    with pytest.raises(BackupError, match="wrong or the file has been modified"):
        inspect_backup(archive, "this password is wrong")

    damaged = bytearray(archive.read_bytes())
    damaged[-1] ^= 1
    archive.write_bytes(damaged)
    with pytest.raises(BackupError, match="wrong or the file has been modified"):
        inspect_backup(archive, PASSWORD)


def test_restore_is_staged_without_overwriting_live_data(
    tmp_path: Path,
    initialized_data: tuple[Path, str],
) -> None:
    archive = make_backup(tmp_path, initialized_data)
    restore_dir = tmp_path / "restore-staging"

    inspection = stage_restore(archive, PASSWORD, restore_dir)

    assert inspection.counts["accounts"] == 1
    assert (restore_dir / "autosign.db").is_file()
    assert (restore_dir / "master-key.env").is_file()
    assert (restore_dir / "RESTORE_INSTRUCTIONS.txt").is_file()
    connection = sqlite3.connect(restore_dir / "autosign.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone() == (1,)
    finally:
        connection.close()

    with pytest.raises(BackupError, match="nothing was overwritten"):
        stage_restore(archive, PASSWORD, restore_dir)


def test_short_password_is_rejected(
    tmp_path: Path,
    initialized_data: tuple[Path, str],
) -> None:
    database_path, key = initialized_data

    with pytest.raises(BackupError, match="at least 12"):
        create_backup(
            database_path=database_path,
            master_key=key,
            password="short",
            output_dir=tmp_path / "backups",
            autosign_version="test-version",
        )
