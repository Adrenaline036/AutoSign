from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.config import Settings
from autosign.core.db import AppMetadata, Database
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountService
from autosign.core.services.monitoring import (
    UPTIME_KUMA_PUSH_URL_SECRET,
    UptimeKumaPushClient,
)
from autosign.core.services.napcat import (
    NAPCAT_BASE_URL_SECRET,
    NAPCAT_TARGET_ID_SECRET,
    NAPCAT_TARGET_TYPE_SECRET,
    NAPCAT_TOKEN_SECRET,
    NapCatClient,
    NapCatConfig,
)
from autosign.core.services.notifications import (
    LEGACY_MIGRATION_COMPLETE,
    LEGACY_MIGRATION_KEY,
    NotificationChannelService,
)
from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus
from autosign.web.app import create_app


class FakeUptimeClient(UptimeKumaPushClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def push(
        self,
        push_url: str,
        *,
        status: str,
        message: str,
        ping_ms: int | None = None,
    ) -> None:
        self.calls.append(
            {
                "push_url": push_url,
                "status": status,
                "message": message,
                "ping_ms": ping_ms,
            }
        )


class FakeNapCatClient(NapCatClient):
    def __init__(self) -> None:
        self.messages: list[tuple[NapCatConfig, str]] = []

    async def send(self, config: NapCatConfig, message: str) -> None:
        self.messages.append((config, message))


@pytest.mark.asyncio
async def test_notification_service_maps_final_results_to_assigned_channels(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'notifications.db').as_posix()}")
    database.migrate()
    account = AccountService(database).create(
        plugin_id="demo",
        label="通知测试",
        enabled=True,
        settings={},
    )
    uptime = FakeUptimeClient()
    napcat = FakeNapCatClient()
    service = NotificationChannelService(
        database,
        SecretCipher(SecretCipher.generate_key()),
        uptime_client=uptime,
        napcat_client=napcat,
    )
    kuma_channel = service.create(
        name="Test Kuma",
        channel_type="uptime_kuma",
        config={"push_url": "https://kuma.example/api/push/secret-token"},
    )
    napcat_channel = service.create(
        name="Test QQ",
        channel_type="napcat",
        config={
            "base_url": "http://napcat.example:3000",
            "access_token": "secret-token",
            "target_type": "private",
            "target_id": "123456789",
        },
    )
    service.assign(account.id, [kuma_channel.id, napcat_channel.id])

    success_deliveries = await service.send_result(
        account.id,
        account_label=account.label,
        plugin_id="demo",
        result=SignResult(
            status=SignStatus.ALREADY_SIGNED,
            message="今日已经签到",
            verified=True,
            duration_ms=321,
        ),
    )
    failure_deliveries = await service.send_result(
        account.id,
        account_label=account.label,
        plugin_id="demo",
        result=SignResult(
            status=SignStatus.INTERACTION_REQUIRED,
            message="需要重新登录",
            verified=False,
        ),
    )

    assert len(success_deliveries) == 2
    assert all(delivery.success for delivery in success_deliveries)
    assert len(failure_deliveries) == 2
    assert all(delivery.success for delivery in failure_deliveries)
    assert uptime.calls[0]["status"] == "up"
    assert uptime.calls[0]["ping_ms"] == 321
    assert uptime.calls[1]["status"] == "down"
    assert "【AutoSign 每日签到】" in napcat.messages[0][1]
    assert "账户：通知测试" in napcat.messages[0][1]
    assert "结果：今日已签到" in napcat.messages[0][1]
    assert "耗时：321 ms" in napcat.messages[0][1]
    assert "结果：需要重新登录" in napcat.messages[1][1]
    database.dispose()


def test_legacy_notification_secrets_are_migrated_and_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    with migrated_database.session() as session:
        marker = session.get(AppMetadata, LEGACY_MIGRATION_KEY)
        assert marker is not None
        assert marker.value == LEGACY_MIGRATION_COMPLETE
    database_text = (tmp_path / "autosign.db").read_bytes().decode(
        "utf-8",
        errors="ignore",
    )
    assert "shared-token" not in database_text
    assert "123456789" not in database_text
    assert "monitor-token" not in database_text
    migrated_database.dispose()

    def fail_if_startup_rescans(*_args, **_kwargs) -> int:
        raise AssertionError("Completed legacy migration must not rescan accounts.")

    monkeypatch.setattr(
        NotificationChannelService,
        "_migrate_legacy_accounts",
        fail_if_startup_rescans,
    )
    with TestClient(create_app(settings)) as client:
        assert len(client.get("/api/v1/notification-channels").json()) == 2


def test_legacy_notification_migration_recovers_after_durable_boundary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = SecretCipher.generate_key()
    database = Database(f"sqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    database.migrate()
    cipher = SecretCipher(key)
    vault = VaultService(database, cipher)
    vault.initialize_key_check()
    account = AccountService(database).create(
        plugin_id="demo",
        label="Recovery",
        enabled=True,
        settings={},
    )
    vault.set(
        account.id,
        UPTIME_KUMA_PUSH_URL_SECRET,
        "https://kuma.example/api/push/recovery-token",
    )
    service = NotificationChannelService(database, cipher)
    delete_many = vault.delete_many

    def fail_before_legacy_secret_delete(_account_id: str, _names: set[str]) -> None:
        raise RuntimeError("injected legacy secret delete failure")

    monkeypatch.setattr(vault, "delete_many", fail_before_legacy_secret_delete)
    with pytest.raises(RuntimeError, match="injected legacy secret delete failure"):
        service.migrate_legacy(vault)

    assert service.legacy_migration_complete() is False
    assert len(service.list()) == 1
    assert len(service.assigned_to_account(account.id)) == 1
    assert UPTIME_KUMA_PUSH_URL_SECRET in vault.list_names(account.id)

    monkeypatch.setattr(vault, "delete_many", delete_many)
    assert service.migrate_legacy(vault) == 1
    assert service.legacy_migration_complete() is True
    assert len(service.list()) == 1
    assert len(service.assigned_to_account(account.id)) == 1
    assert UPTIME_KUMA_PUSH_URL_SECRET not in vault.list_names(account.id)
    assert service.migrate_legacy(vault) == 0

    vault.set(
        account.id,
        UPTIME_KUMA_PUSH_URL_SECRET,
        "https://kuma.example/api/push/recovery-token",
    )
    monkeypatch.setattr(vault, "delete_many", fail_before_legacy_secret_delete)
    with pytest.raises(RuntimeError, match="injected legacy secret delete failure"):
        service.migrate_legacy(vault, force=True)
    assert service.legacy_migration_complete() is False
    assert UPTIME_KUMA_PUSH_URL_SECRET in vault.list_names(account.id)

    monkeypatch.setattr(vault, "delete_many", delete_many)
    assert service.migrate_legacy(vault) == 1
    assert service.legacy_migration_complete() is True
    assert len(service.list()) == 1
    assert UPTIME_KUMA_PUSH_URL_SECRET not in vault.list_names(account.id)
    database.dispose()


def test_application_startup_retries_legacy_migration_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        label="Startup recovery",
        enabled=True,
        settings={},
    )
    vault.set(
        account.id,
        UPTIME_KUMA_PUSH_URL_SECRET,
        "https://kuma.example/api/push/startup-recovery-token",
    )
    database.dispose()

    delete_many = VaultService.delete_many

    def fail_startup_delete(
        _vault: VaultService,
        _account_id: str,
        _names: set[str],
    ) -> None:
        raise RuntimeError("injected startup migration failure")

    monkeypatch.setattr(VaultService, "delete_many", fail_startup_delete)
    with pytest.raises(RuntimeError, match="injected startup migration failure"):
        with TestClient(create_app(settings)):
            pass

    failed_database = Database(settings.database_url)
    failed_vault = VaultService(failed_database, cipher)
    failed_service = NotificationChannelService(failed_database, cipher)
    assert failed_service.legacy_migration_complete() is False
    assert UPTIME_KUMA_PUSH_URL_SECRET in failed_vault.list_names(account.id)
    failed_database.dispose()

    monkeypatch.setattr(VaultService, "delete_many", delete_many)
    with TestClient(create_app(settings)) as client:
        channels = client.get("/api/v1/notification-channels").json()
        assert len(channels) == 1

    recovered_database = Database(settings.database_url)
    recovered_vault = VaultService(recovered_database, cipher)
    recovered_service = NotificationChannelService(recovered_database, cipher)
    assert recovered_service.legacy_migration_complete() is True
    assert UPTIME_KUMA_PUSH_URL_SECRET not in recovered_vault.list_names(account.id)
    assert len(recovered_service.list()) == 1
    recovered_database.dispose()
