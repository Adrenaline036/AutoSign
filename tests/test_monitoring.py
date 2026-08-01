from __future__ import annotations

from pathlib import Path

import pytest

from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountService
from autosign.core.services.monitoring import (
    MonitoringService,
    UptimeKumaPushClient,
)
from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus


class FakePushClient(UptimeKumaPushClient):
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


def monitor_service(tmp_path: Path) -> tuple[MonitoringService, FakePushClient, str]:
    database = Database(f"sqlite:///{(tmp_path / 'monitor.db').as_posix()}")
    database.migrate()
    account = AccountService(database).create(
        plugin_id="demo",
        label="监控测试",
        enabled=True,
        settings={},
    )
    vault = VaultService(database, SecretCipher(SecretCipher.generate_key()))
    vault.initialize_key_check()
    client = FakePushClient()
    service = MonitoringService(vault, client)
    service.set_push_url(account.id, "https://kuma.example/api/push/secret-token")
    return service, client, account.id


def test_push_url_builder_overwrites_status_fields() -> None:
    url = UptimeKumaPushClient.build_url(
        "https://kuma.example/api/push/token?status=down&custom=keep",
        status="up",
        message="签到成功",
        ping_ms=123,
    )
    assert "status=up" in url
    assert "status=down" not in url
    assert "custom=keep" in url
    assert "ping=123" in url


@pytest.mark.asyncio
async def test_monitoring_maps_final_result_to_kuma_status(tmp_path: Path) -> None:
    service, client, account_id = monitor_service(tmp_path)

    success = await service.send_result(
        account_id,
        SignResult(
            status=SignStatus.ALREADY_SIGNED,
            message="今日已签到",
            verified=True,
            duration_ms=321,
        ),
    )
    failure = await service.send_result(
        account_id,
        SignResult(
            status=SignStatus.INTERACTION_REQUIRED,
            message="需要重新登录",
            verified=False,
        ),
    )

    assert success.success is True
    assert failure.success is True
    assert client.calls[0]["status"] == "up"
    assert client.calls[0]["ping_ms"] == 321
    assert client.calls[1]["status"] == "down"
