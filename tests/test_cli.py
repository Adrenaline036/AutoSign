from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

import autosign.__main__ as cli
from autosign.core.config import Settings
from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountService
from autosign.core.services.monitoring import UPTIME_KUMA_PUSH_URL_SECRET
from autosign.core.services.notifications import NotificationChannelService
from autosign.core.services.vault import VaultService


def test_initialize_master_key_fills_empty_example_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AUTOSIGN_PORT=8000\nAUTOSIGN_MASTER_KEY=\n", encoding="utf-8")

    assert cli.initialize_master_key(env_path) == 0

    contents = env_path.read_text(encoding="utf-8")
    assert "AUTOSIGN_PORT=8000" in contents
    assert "AUTOSIGN_MASTER_KEY=\n" not in contents
    assert len(contents.partition("AUTOSIGN_MASTER_KEY=")[2].strip()) > 20


def test_initialize_master_key_does_not_replace_existing_value(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AUTOSIGN_MASTER_KEY=already-set\n", encoding="utf-8")

    assert cli.initialize_master_key(env_path) == 0
    assert env_path.read_text(encoding="utf-8") == "AUTOSIGN_MASTER_KEY=already-set\n"


def test_repair_legacy_notifications_command_forces_a_completed_scan(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    key = SecretCipher.generate_key()
    settings = Settings(
        environment="testing",
        data_dir=tmp_path,
        master_key=SecretStr(key),
        auth_disabled=True,
    )
    settings.prepare_directories()
    database = Database(settings.database_url)
    database.migrate()
    cipher = SecretCipher(key)
    vault = VaultService(database, cipher)
    vault.initialize_key_check()
    account = AccountService(database).create(
        plugin_id="demo",
        label="CLI repair",
        enabled=True,
        settings={},
    )
    service = NotificationChannelService(database, cipher)
    assert service.migrate_legacy(vault) == 0
    vault.set(
        account.id,
        UPTIME_KUMA_PUSH_URL_SECRET,
        "https://kuma.example/api/push/cli-repair-token",
    )
    assert service.migrate_legacy(vault) == 0
    database.dispose()

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    arguments = cli._parser().parse_args(["repair-legacy-notifications"])

    assert cli._run_command(arguments) == 0
    assert "migrated=1" in capsys.readouterr().out

    repaired_database = Database(settings.database_url)
    repaired_vault = VaultService(repaired_database, cipher)
    repaired_service = NotificationChannelService(repaired_database, cipher)
    assert repaired_service.legacy_migration_complete() is True
    assert len(repaired_service.list()) == 1
    assert UPTIME_KUMA_PUSH_URL_SECRET not in repaired_vault.list_names(account.id)
    repaired_database.dispose()
