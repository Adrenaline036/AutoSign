from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from autosign.core.security import SecretCipher, SecretConfigurationError, SecretDecryptionError
from autosign.core.services.vault import KEY_CHECK_AAD, KEY_CHECK_NAME, KEY_CHECK_PLAINTEXT

BACKUP_MAGIC = b"AUTOSIGN-BACKUP-V1\n"
BACKUP_FORMAT_VERSION = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KDF_N = 2**15
KDF_R = 8
KDF_P = 1
REQUIRED_TABLES = {
    "accounts",
    "account_secrets",
    "schedules",
    "execution_records",
    "notification_channels",
    "account_notification_channels",
    "app_metadata",
    "alembic_version",
}


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupInspection:
    created_at: str
    autosign_version: str
    schema_version: str
    counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _BundleContents:
    database: bytes
    master_key_env: bytes
    manifest: dict[str, Any]


def create_backup(
    *,
    database_path: Path,
    master_key: str,
    password: str,
    output_dir: Path,
    autosign_version: str,
    filename_prefix: str = "autosign",
) -> Path:
    _validate_password(password)
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise BackupError(f"Database file does not exist: {database_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="autosign-backup-") as temporary:
        snapshot_path = Path(temporary) / "autosign.db"
        _snapshot_database(database_path, snapshot_path)
        database_bytes = snapshot_path.read_bytes()

    master_key_env = f"AUTOSIGN_MASTER_KEY={master_key}\n".encode()
    validation = _validate_database(database_bytes, master_key)
    created_at = datetime.now(UTC)
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": created_at.isoformat(),
        "autosign_version": autosign_version,
        "schema_version": validation["schema_version"],
        "counts": validation["counts"],
        "files": {
            "autosign.db": _sha256(database_bytes),
            "master-key.env": _sha256(master_key_env),
        },
    }
    archive_plaintext = _build_zip(database_bytes, master_key_env, manifest)
    encrypted = _encrypt(archive_plaintext, password)
    timestamp = created_at.strftime("%Y%m%d-%H%M%S-%f")
    if not filename_prefix or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in filename_prefix
    ):
        raise BackupError("Backup filename prefix contains unsupported characters.")
    destination = output_dir.resolve() / f"{filename_prefix}-{timestamp}.asbackup"
    if destination.exists():
        raise BackupError(f"Backup already exists: {destination}")
    _atomic_write(destination, encrypted, mode=0o600)
    return destination


def inspect_backup(archive_path: Path, password: str) -> BackupInspection:
    contents = _read_bundle(archive_path, password)
    return _inspect_contents(contents)


def stage_restore(archive_path: Path, password: str, output_dir: Path) -> BackupInspection:
    contents = _read_bundle(archive_path, password)
    inspection = _inspect_contents(contents)
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise BackupError(f"Restore target already exists; nothing was overwritten: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        _write_private(temporary / "autosign.db", contents.database)
        _write_private(temporary / "master-key.env", contents.master_key_env)
        instructions = (
            "AutoSign 安全恢复暂存目录\n\n"
            "此操作没有覆盖正在运行的数据。请先停止 AutoSign 容器，再备份当前 data 和 .env，\n"
            "然后将 autosign.db 放入正式 data 目录，并把 master-key.env 中的\n"
            "AUTOSIGN_MASTER_KEY 写入正式 .env。启动后先检查 /healthz，再执行手动签到。\n"
        ).encode()
        _write_private(temporary / "RESTORE_INSTRUCTIONS.txt", instructions)
        os.replace(temporary, output_dir)
    except Exception:
        if temporary.exists():
            for child in temporary.iterdir():
                child.unlink(missing_ok=True)
            temporary.rmdir()
        raise
    return inspection


def _inspect_contents(contents: _BundleContents) -> BackupInspection:
    master_key = _parse_master_key(contents.master_key_env)
    validation = _validate_database(contents.database, master_key)
    manifest = contents.manifest
    if validation["schema_version"] != manifest.get("schema_version"):
        raise BackupError("Backup database schema does not match its manifest.")
    if validation["counts"] != manifest.get("counts"):
        raise BackupError("Backup database counts do not match its manifest.")
    return BackupInspection(
        created_at=str(manifest["created_at"]),
        autosign_version=str(manifest["autosign_version"]),
        schema_version=str(manifest["schema_version"]),
        counts=dict(manifest["counts"]),
    )


def _snapshot_database(source_path: Path, destination_path: Path) -> None:
    try:
        source = sqlite3.connect(
            f"file:{source_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        destination = sqlite3.connect(destination_path)
        try:
            source.execute("PRAGMA busy_timeout=5000")
            source.execute("PRAGMA query_only=ON")
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to create SQLite snapshot: {exc}") from exc


def _validate_database(database_bytes: bytes, master_key: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="autosign-validate-") as temporary:
        database_path = Path(temporary) / "autosign.db"
        database_path.write_bytes(database_bytes)
        try:
            connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise BackupError("SQLite quick_check did not return ok.")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                missing = REQUIRED_TABLES - tables
                if missing:
                    raise BackupError(
                        "Backup database is missing required tables: " + ", ".join(sorted(missing))
                    )
                schema_row = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                if schema_row is None:
                    raise BackupError("Backup database has no schema version.")
                key_check_row = connection.execute(
                    "SELECT value FROM app_metadata WHERE key = ?", (KEY_CHECK_NAME,)
                ).fetchone()
                if key_check_row is None:
                    raise BackupError("Backup database has no master-key verification record.")
                counts = {
                    table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in (
                        "accounts",
                        "account_secrets",
                        "schedules",
                        "execution_records",
                        "notification_channels",
                        "account_notification_channels",
                    )
                }
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise BackupError(f"Backup database validation failed: {exc}") from exc

    try:
        plaintext = SecretCipher(master_key).decrypt(
            key_check_row[0], associated_data=KEY_CHECK_AAD
        )
    except (SecretConfigurationError, SecretDecryptionError) as exc:
        raise BackupError("The backup master key does not match the database.") from exc
    if plaintext != KEY_CHECK_PLAINTEXT:
        raise BackupError("The backup master-key verification value is invalid.")
    return {"schema_version": str(schema_row[0]), "counts": counts}


def _build_zip(database: bytes, master_key_env: bytes, manifest: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("autosign.db", database)
        archive.writestr("master-key.env", master_key_env)
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return buffer.getvalue()


def _read_bundle(archive_path: Path, password: str) -> _BundleContents:
    _validate_password(password)
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise BackupError(f"Backup file does not exist: {archive_path}")
    plaintext = _decrypt(archive_path.read_bytes(), password)
    try:
        with zipfile.ZipFile(io.BytesIO(plaintext), "r") as archive:
            expected = {"autosign.db", "master-key.env", "manifest.json"}
            if set(archive.namelist()) != expected:
                raise BackupError("Backup contains an unexpected file set.")
            database = archive.read("autosign.db")
            master_key_env = archive.read("master-key.env")
            manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError("Backup payload is damaged or malformed.") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("Unsupported backup format version.")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BackupError("Backup manifest has no file checksums.")
    if files.get("autosign.db") != _sha256(database):
        raise BackupError("Backup database checksum does not match.")
    if files.get("master-key.env") != _sha256(master_key_env):
        raise BackupError("Backup master-key checksum does not match.")
    return _BundleContents(database=database, master_key_env=master_key_env, manifest=manifest)


def _parse_master_key(contents: bytes) -> str:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError("Backup master-key file is not valid UTF-8.") from exc
    lines = [line for line in text.splitlines() if line.startswith("AUTOSIGN_MASTER_KEY=")]
    if len(lines) != 1:
        raise BackupError("Backup master-key file is malformed.")
    key = lines[0].split("=", 1)[1].strip()
    if not key:
        raise BackupError("Backup master key is empty.")
    return key


def _derive_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=KDF_N, r=KDF_R, p=KDF_P).derive(
        password.encode("utf-8")
    )


def _encrypt(plaintext: bytes, password: str) -> bytes:
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_derive_key(password, salt)).encrypt(nonce, plaintext, BACKUP_MAGIC)
    return BACKUP_MAGIC + salt + nonce + ciphertext


def _decrypt(payload: bytes, password: str) -> bytes:
    minimum = len(BACKUP_MAGIC) + SALT_BYTES + NONCE_BYTES + 16
    if len(payload) < minimum or not payload.startswith(BACKUP_MAGIC):
        raise BackupError("This is not a supported AutoSign backup file.")
    offset = len(BACKUP_MAGIC)
    salt = payload[offset : offset + SALT_BYTES]
    offset += SALT_BYTES
    nonce = payload[offset : offset + NONCE_BYTES]
    ciphertext = payload[offset + NONCE_BYTES :]
    try:
        return AESGCM(_derive_key(password, salt)).decrypt(nonce, ciphertext, BACKUP_MAGIC)
    except InvalidTag as exc:
        raise BackupError("Backup password is wrong or the file has been modified.") from exc


def _validate_password(password: str) -> None:
    if len(password) < 12:
        raise BackupError("Backup password must contain at least 12 characters.")


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _atomic_write(path: Path, contents: bytes, *, mode: int) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_private(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    os.chmod(path, 0o600)
