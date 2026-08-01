from __future__ import annotations

from pathlib import Path

import pytest

from autosign.core.db import Database
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountService
from autosign.core.services.napcat import NapCatClient, NapCatConfig, NapCatService
from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus


class FakeNapCatClient(NapCatClient):
    def __init__(self) -> None:
        self.messages: list[tuple[NapCatConfig, str]] = []

    async def send(self, config: NapCatConfig, message: str) -> None:
        self.messages.append((config, message))


def napcat_service(tmp_path: Path) -> tuple[NapCatService, FakeNapCatClient, str]:
    database = Database(f"sqlite:///{(tmp_path / 'napcat.db').as_posix()}")
    database.migrate()
    account = AccountService(database).create(
        plugin_id="demo",
        label="我的账户",
        enabled=True,
        settings={},
    )
    vault = VaultService(database, SecretCipher(SecretCipher.generate_key()))
    vault.initialize_key_check()
    client = FakeNapCatClient()
    service = NapCatService(vault, client)
    service.configure(
        account.id,
        base_url="http://napcat.example:3000",
        access_token="secret-token",
        target_type="private",
        target_id="123456789",
    )
    return service, client, account.id


def test_napcat_config_validation() -> None:
    with pytest.raises(ValueError, match="access token"):
        NapCatClient.validate_config(
            base_url="http://napcat.example:3000",
            access_token="",
            target_type="private",
            target_id="123456789",
        )
    with pytest.raises(ValueError, match="5-20 digits"):
        NapCatClient.validate_config(
            base_url="http://napcat.example:3000",
            access_token="token",
            target_type="group",
            target_id="not-a-group",
        )


@pytest.mark.asyncio
async def test_napcat_sends_one_final_result_message(tmp_path: Path) -> None:
    service, client, account_id = napcat_service(tmp_path)
    delivery = await service.send_result(
        account_id,
        account_label="我的账户",
        plugin_id="yamibo",
        result=SignResult(
            status=SignStatus.ALREADY_SIGNED,
            message="今日已经打过卡",
            verified=True,
            duration_ms=456,
        ),
    )

    assert delivery.success is True
    assert len(client.messages) == 1
    config, message = client.messages[0]
    assert config.target_id == "123456789"
    assert "【AutoSign 每日签到】" in message
    assert "账户：我的账户" in message
    assert "结果：今日已签到" in message
    assert "耗时：456 ms" in message
