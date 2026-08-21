from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from autosign import __version__
from autosign.core.backup import BackupError, create_backup, inspect_backup, stage_restore
from autosign.core.config import get_settings
from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.notifications import NotificationChannelService
from autosign.core.services.vault import VaultService


def initialize_master_key(env_path: Path = Path(".env")) -> int:
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("AUTOSIGN_MASTER_KEY="):
            continue
        if line.partition("=")[2].strip():
            print(f"Master key already exists in {env_path.resolve()}.")
            return 0
        lines[index] = f"AUTOSIGN_MASTER_KEY={SecretCipher.generate_key()}"
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Created a new master key in {env_path.resolve()}. Keep this file private.")
        return 0

    separator = "" if not existing or existing.endswith("\n") else "\n"
    updated = f"{existing}{separator}AUTOSIGN_MASTER_KEY={SecretCipher.generate_key()}\n"
    env_path.write_text(updated, encoding="utf-8")
    print(f"Created a new master key in {env_path.resolve()}. Keep this file private.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m autosign")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("init-key", help="Create AUTOSIGN_MASTER_KEY in .env.")
    subcommands.add_parser(
        "repair-legacy-notifications",
        help="Force a one-time repair of pre-channel notification secrets.",
    )
    for command, help_text in (
        ("backup", "Create an encrypted database and master-key backup."),
        ("backup-check", "Decrypt and validate an AutoSign backup."),
        ("restore", "Validate and unpack a backup into a new staging directory."),
    ):
        child = subcommands.add_parser(command, help=help_text)
        child.add_argument("--password-file", type=Path)
        if command == "backup":
            child.add_argument("--output-dir", type=Path)
        else:
            child.add_argument("archive", type=Path)
        if command == "restore":
            child.add_argument("--output-dir", type=Path)
    return parser


def _backup_password(password_file: Path | None, *, confirm: bool) -> str:
    if password_file is not None:
        try:
            password = password_file.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise BackupError(f"Unable to read backup password file: {password_file}") from exc
    elif os.environ.get("AUTOSIGN_BACKUP_PASSWORD"):
        password = os.environ["AUTOSIGN_BACKUP_PASSWORD"]
    elif sys.stdin.isatty():
        password = getpass.getpass("备份密码（至少12个字符）：")
        if confirm and password != getpass.getpass("再次输入备份密码："):
            raise BackupError("The two backup passwords do not match.")
    else:
        raise BackupError(
            "No interactive terminal is available. Use --password-file or "
            "AUTOSIGN_BACKUP_PASSWORD."
        )
    return password.rstrip("\r\n")


def _run_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "init-key":
        return initialize_master_key()
    settings = get_settings()
    settings.prepare_directories()
    if arguments.command == "repair-legacy-notifications":
        database = Database(
            settings.database_url,
            sqlite_busy_timeout_ms=settings.database_busy_timeout_ms,
        )
        try:
            database.migrate()
            cipher = SecretCipher(settings.require_master_key())
            vault = VaultService(database, cipher)
            vault.initialize_key_check()
            migrated = NotificationChannelService(database, cipher).migrate_legacy(
                vault,
                force=True,
            )
        finally:
            database.dispose()
        print(f"Legacy notification repair completed: migrated={migrated}")
        return 0
    password = _backup_password(arguments.password_file, confirm=arguments.command == "backup")
    if arguments.command == "backup":
        output_dir = arguments.output_dir or settings.data_dir / "backups"
        destination = create_backup(
            database_path=settings.data_dir / "autosign.db",
            master_key=settings.require_master_key(),
            password=password,
            output_dir=output_dir,
            autosign_version=__version__,
        )
        print(f"Encrypted backup created: {destination}")
        return 0
    if arguments.command == "backup-check":
        inspection = inspect_backup(arguments.archive, password)
        print(
            "Backup is valid: "
            f"created={inspection.created_at}, autosign={inspection.autosign_version}, "
            f"schema={inspection.schema_version}, counts={inspection.counts}"
        )
        return 0
    if arguments.command == "restore":
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        output_dir = arguments.output_dir or settings.data_dir / "restores" / timestamp
        inspection = stage_restore(arguments.archive, password, output_dir)
        print(f"Validated restore staged without overwriting live data: {output_dir.resolve()}")
        print(f"Backup created at {inspection.created_at}; read RESTORE_INSTRUCTIONS.txt.")
        return 0
    raise BackupError("Unknown command.")


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command is not None:
        try:
            raise SystemExit(_run_command(arguments))
        except BackupError as exc:
            raise SystemExit(f"Backup operation failed safely: {exc}") from exc

    settings = get_settings()
    uvicorn.run(
        "autosign.web.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=settings.environment == "development",
    )


if __name__ == "__main__":
    main()
