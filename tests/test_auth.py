from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.auth import PASSWORD_HASH_KEY
from autosign.core.config import Settings
from autosign.core.security import SecretCipher
from autosign.web.app import create_app

ADMIN_PASSWORD = "correct horse battery staple"


def auth_settings(data_dir: Path) -> Settings:
    return Settings(
        environment="testing",
        data_dir=data_dir,
        master_key=SecretStr(SecretCipher.generate_key()),
    )


def test_first_run_setup_login_csrf_and_logout(tmp_path: Path) -> None:
    with TestClient(create_app(auth_settings(tmp_path))) as client:
        first_page = client.get("/")
        assert "设置管理员密码" in first_page.text
        assert 'fetch("/api/v1/auth/status", {cache: "no-store"})' in first_page.text
        assert "no-store" in first_page.headers["cache-control"]
        assert first_page.headers["vary"] == "Cookie"
        first_status = client.get("/api/v1/auth/status")
        assert "no-store" in first_status.headers["cache-control"]
        assert first_status.headers["vary"] == "Cookie"
        assert client.get("/api/v1/accounts").status_code == 401

        setup = client.post(
            "/api/v1/auth/setup",
            json={"password": ADMIN_PASSWORD},
        )
        assert setup.status_code == 200
        assert setup.json()["authenticated"] is True
        assert "HttpOnly" in setup.headers["set-cookie"]
        assert "SameSite=strict" in setup.headers["set-cookie"]
        csrf_token = setup.json()["csrf_token"]

        authenticated_page = client.get("/")
        assert authenticated_page.status_code == 200
        assert "<title>AutoSign</title>" in authenticated_page.text
        assert "no-store" in authenticated_page.headers["cache-control"]
        assert client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "blocked"},
        ).status_code == 403

        created = client.post(
            "/api/v1/accounts",
            headers={"X-AutoSign-CSRF": csrf_token},
            json={"plugin_id": "demo", "label": "authenticated"},
        )
        assert created.status_code == 201

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-AutoSign-CSRF": csrf_token},
            json={},
        )
        assert logout.status_code == 200
        assert client.get("/api/v1/accounts").status_code == 401

        wrong = client.post(
            "/api/v1/auth/login",
            json={"password": "this password is wrong"},
        )
        assert wrong.status_code == 401
        login = client.post(
            "/api/v1/auth/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        assert client.get("/api/v1/accounts").status_code == 200

    with sqlite3.connect(tmp_path / "autosign.db") as connection:
        stored_hash = connection.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (PASSWORD_HASH_KEY,),
        ).fetchone()[0]
    assert stored_hash.startswith("scrypt-v1$")
    assert ADMIN_PASSWORD not in stored_hash
    assert ADMIN_PASSWORD.encode("utf-8") not in (tmp_path / "autosign.db").read_bytes()


def test_setup_cannot_replace_existing_password(tmp_path: Path) -> None:
    with TestClient(create_app(auth_settings(tmp_path))) as client:
        assert client.post(
            "/api/v1/auth/setup",
            json={"password": ADMIN_PASSWORD},
        ).status_code == 200
        repeated = client.post(
            "/api/v1/auth/setup",
            json={"password": "another secure administrator password"},
        )
        assert repeated.status_code == 409
