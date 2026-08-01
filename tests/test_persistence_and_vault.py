from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.config import Settings
from autosign.core.security import SecretCipher, SecretDecryptionError
from autosign.web.app import create_app


def make_settings(data_dir: Path, key: str) -> Settings:
    return Settings(
        environment="testing",
        data_dir=data_dir,
        master_key=SecretStr(key),
        auth_disabled=True,
    )


def test_account_and_encrypted_secret_survive_restart(tmp_path: Path) -> None:
    key = SecretCipher.generate_key()
    plaintext = "BDUSS=this-must-never-be-stored-in-plaintext"
    settings = make_settings(tmp_path, key)

    with TestClient(create_app(settings)) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "持久化测试"},
        ).json()
        response = client.put(
            f"/api/v1/accounts/{account['id']}/secrets/test_cookie",
            json={"value": plaintext},
        )
        assert response.status_code == 200
        assert response.json() == {"names": ["test_cookie"]}
        assert plaintext not in response.text
        assert client.app.state.vault.get(account["id"], "test_cookie") == plaintext

    database_path = tmp_path / "autosign.db"
    database_bytes = database_path.read_bytes()
    assert plaintext.encode("utf-8") not in database_bytes

    with sqlite3.connect(database_path) as connection:
        stored_value = connection.execute(
            "SELECT encrypted_value FROM account_secrets WHERE account_id = ? AND name = ?",
            (account["id"], "test_cookie"),
        ).fetchone()[0]
    assert stored_value != plaintext

    with TestClient(create_app(make_settings(tmp_path, key))) as restarted_client:
        accounts = restarted_client.get("/api/v1/accounts").json()
        assert len(accounts) == 1
        assert accounts[0]["label"] == "持久化测试"
        assert accounts[0]["secret_names"] == ["test_cookie"]
        assert plaintext not in restarted_client.get("/api/v1/accounts").text


def test_wrong_master_key_stops_application_startup(tmp_path: Path) -> None:
    correct_key = SecretCipher.generate_key()
    with TestClient(create_app(make_settings(tmp_path, correct_key))) as client:
        assert client.get("/healthz").status_code == 200

    wrong_key = SecretCipher.generate_key()
    with pytest.raises(SecretDecryptionError):
        with TestClient(create_app(make_settings(tmp_path, wrong_key))):
            pass


def test_missing_master_key_is_rejected(tmp_path: Path) -> None:
    settings = Settings(
        environment="testing",
        data_dir=tmp_path,
        master_key=None,
        auth_disabled=True,
    )
    with pytest.raises(RuntimeError, match="AUTOSIGN_MASTER_KEY is required"):
        create_app(settings)


def test_deleting_account_cascades_to_secrets(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, SecretCipher.generate_key())
    with TestClient(create_app(settings)) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "待删除账户"},
        ).json()
        client.put(
            f"/api/v1/accounts/{account['id']}/secrets/token",
            json={"value": "top-secret"},
        )
        deleted = client.post(
            f"/api/v1/accounts/{account['id']}/delete",
            json={"confirm_label": "待删除账户"},
        )
        assert deleted.status_code == 204

    with sqlite3.connect(tmp_path / "autosign.db") as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM account_secrets WHERE account_id = ?",
            (account["id"],),
        ).fetchone()[0]
    assert count == 0
