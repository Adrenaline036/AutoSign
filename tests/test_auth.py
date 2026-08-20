from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

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


def test_first_run_setup_login_csrf_and_logout(tmp_path: Path, caplog) -> None:
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
        assert client.get("/api/v1/backups/status").status_code == 401
        assert client.get("/api/v1/system/status").status_code == 401
        assert client.get("/api/v1/executions").status_code == 401
        assert client.get("/api/v1/notification-channels").status_code == 401
        assert client.get("/assets/accounts.js").status_code == 401
        assert client.get("/assets/history.js").status_code == 401
        assert client.get("/assets/browser.js").status_code == 401
        assert client.get("/assets/vikacg-recovery.js").status_code == 401

        setup = client.post(
            "/api/v1/auth/setup",
            json={"password": ADMIN_PASSWORD},
        )
        assert setup.status_code == 200
        assert setup.json()["authenticated"] is True
        assert "HttpOnly" in setup.headers["set-cookie"]
        assert "SameSite=strict" in setup.headers["set-cookie"]
        csrf_token = setup.json()["csrf_token"]

        backup_without_csrf = client.post("/api/v1/backups/run", json={})
        assert backup_without_csrf.status_code == 403
        notification_without_csrf = client.post(
            "/api/v1/notification-channels",
            json={},
        )
        assert notification_without_csrf.status_code == 403
        account_secret_without_csrf = client.put(
            "/api/v1/accounts/unknown/secrets/token",
            json={"value": "must-not-be-saved"},
        )
        assert account_secret_without_csrf.status_code == 403
        schedule_without_csrf = client.put(
            "/api/v1/accounts/unknown/schedule",
            json={"daily_time": "08:30"},
        )
        assert schedule_without_csrf.status_code == 403

        paste_without_csrf = client.post(
            "/api/v1/browser-sessions/unknown/type",
            json={"text": "must-not-be-logged"},
        )
        assert paste_without_csrf.status_code == 403
        activity_without_csrf = client.post(
            "/api/v1/browser-sessions/unknown/activity",
        )
        assert activity_without_csrf.status_code == 403
        recovery_without_csrf = client.post(
            "/api/v1/accounts/unknown/vikacg-state-import",
            json={"raw_json": "{}", "confirm_overwrite": False},
        )
        assert recovery_without_csrf.status_code == 403
        recovery_unknown_account = client.post(
            "/api/v1/accounts/unknown/vikacg-state-import",
            headers={"X-AutoSign-CSRF": csrf_token},
            json={"raw_json": "{}", "confirm_overwrite": False},
        )
        assert recovery_unknown_account.status_code == 404
        paste_unknown_session = client.post(
            "/api/v1/browser-sessions/unknown/type",
            headers={"X-AutoSign-CSRF": csrf_token},
            json={"text": "must-not-be-logged"},
        )
        assert paste_unknown_session.status_code == 404
        assert "must-not-be-logged" not in caplog.text

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
        assert "Path=/" in logout.headers["set-cookie"]
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


def test_auth_router_preserves_cookie_and_direct_peer_limits(tmp_path: Path) -> None:
    settings = auth_settings(tmp_path / "secured")
    settings.auth_secure_cookie = True

    with TestClient(create_app(settings)) as client:
        not_configured = client.post(
            "/api/v1/auth/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert not_configured.status_code == 409

        setup = client.post(
            "/api/v1/auth/setup",
            json={"password": ADMIN_PASSWORD},
        )
        cookie = setup.headers["set-cookie"]
        assert "Max-Age=43200" in cookie
        assert "HttpOnly" in cookie
        assert "Path=/" in cookie
        assert "SameSite=strict" in cookie
        assert "Secure" in cookie

        for attempt in range(5):
            wrong = client.post(
                "/api/v1/auth/login",
                headers={"X-Forwarded-For": f"198.51.100.{attempt + 1}"},
                json={"password": "this password is wrong"},
            )
            assert wrong.status_code == 401
        limited = client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-For": "203.0.113.200"},
            json={"password": ADMIN_PASSWORD},
        )
        assert limited.status_code == 429

    disabled_settings = auth_settings(tmp_path / "disabled")
    disabled_settings.auth_disabled = True
    with TestClient(create_app(disabled_settings)) as client:
        disabled_setup = client.post(
            "/api/v1/auth/setup",
            json={"password": ADMIN_PASSWORD},
        )
        assert disabled_setup.status_code == 409


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


def test_live_browser_assets_and_websocket_require_admin_session(tmp_path: Path) -> None:
    novnc_root = tmp_path / "novnc"
    (novnc_root / "core").mkdir(parents=True)
    (novnc_root / "core" / "rfb.js").write_text("export default class RFB {}")
    settings = auth_settings(tmp_path / "data")
    settings.browser_live_enabled = True
    settings.browser_novnc_root = novnc_root

    with TestClient(create_app(settings)) as client:
        assert client.get("/novnc/core/rfb.js").status_code == 401
        with pytest.raises(WebSocketDisconnect) as disconnected:
            with client.websocket_connect(
                "/api/v1/browser-sessions/unknown/vnc",
                headers={"origin": "http://testserver"},
                subprotocols=["binary"],
            ):
                pass
        assert disconnected.value.code == 4401


def test_vnc_websocket_is_unavailable_when_live_transport_is_disabled(
    tmp_path: Path,
) -> None:
    settings = Settings(
        environment="testing",
        data_dir=tmp_path,
        master_key=SecretStr(SecretCipher.generate_key()),
        auth_disabled=True,
    )

    with TestClient(create_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            with client.websocket_connect(
                "/api/v1/browser-sessions/unknown/vnc",
                headers={"origin": "http://testserver"},
                subprotocols=["binary"],
            ):
                pass

        assert disconnected.value.code == 4404
