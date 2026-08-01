from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.config import Settings
from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountService
from autosign.core.services.monitoring import UPTIME_KUMA_PUSH_URL_SECRET
from autosign.core.services.napcat import (
    NAPCAT_BASE_URL_SECRET,
    NAPCAT_TARGET_ID_SECRET,
    NAPCAT_TARGET_TYPE_SECRET,
    NAPCAT_TOKEN_SECRET,
)
from autosign.core.services.vault import VaultService
from autosign.web.app import create_app


def test_legacy_notification_secrets_are_migrated_and_deduplicated(
    tmp_path: Path,
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
    accounts = AccountService(database)
    first = accounts.create(
        plugin_id="demo",
        label="First",
        enabled=True,
        settings={},
    )
    second = accounts.create(
        plugin_id="demo",
        label="Second",
        enabled=True,
        settings={},
    )
    shared_napcat = {
        NAPCAT_BASE_URL_SECRET: "http://192.0.2.10:3000",
        NAPCAT_TOKEN_SECRET: "shared-token",
        NAPCAT_TARGET_TYPE_SECRET: "private",
        NAPCAT_TARGET_ID_SECRET: "123456789",
    }
    for account in (first, second):
        for name, value in shared_napcat.items():
            vault.set(account.id, name, value)
    vault.set(
        first.id,
        UPTIME_KUMA_PUSH_URL_SECRET,
        "https://kuma.example/api/push/monitor-token",
    )
    vault.set(first.id, "browser_storage_state", '{"cookies": []}')
    database.dispose()

    with TestClient(create_app(settings)) as client:
        channels = client.get("/api/v1/notification-channels").json()
        accounts_after = client.get("/api/v1/accounts").json()

    assert len(channels) == 2
    napcat = next(item for item in channels if item["channel_type"] == "napcat")
    kuma = next(item for item in channels if item["channel_type"] == "uptime_kuma")
    assert set(napcat["assigned_account_ids"]) == {first.id, second.id}
    assert kuma["assigned_account_ids"] == [first.id]
    first_after = next(item for item in accounts_after if item["id"] == first.id)
    second_after = next(item for item in accounts_after if item["id"] == second.id)
    assert first_after["monitor_configured"] is True
    assert first_after["napcat_configured"] is True
    assert second_after["monitor_configured"] is False
    assert second_after["napcat_configured"] is True

    migrated_database = Database(settings.database_url)
    migrated_vault = VaultService(migrated_database, cipher)
    assert migrated_vault.list_names(first.id) == ["browser_storage_state"]
    assert migrated_vault.list_names(second.id) == []
    database_text = (tmp_path / "autosign.db").read_bytes().decode(
        "utf-8",
        errors="ignore",
    )
    assert "shared-token" not in database_text
    assert "123456789" not in database_text
    assert "monitor-token" not in database_text
    migrated_database.dispose()

    with TestClient(create_app(settings)) as client:
        assert len(client.get("/api/v1/notification-channels").json()) == 2
