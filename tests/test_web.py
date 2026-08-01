from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core import backup
from autosign.core.config import Settings
from autosign.core.security import SecretCipher
from autosign.web.app import create_app


def settings_for_test(data_dir: Path, key: str | None = None) -> Settings:
    return Settings(
        environment="testing",
        data_dir=data_dir,
        master_key=SecretStr(key or SecretCipher.generate_key()),
        auth_disabled=True,
    )


def test_health_and_plugin_execution(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for_test(tmp_path))) as client:
        dashboard = client.get("/")
        health = client.get("/healthz")
        plugins = client.get("/api/v1/plugins")
        execution = client.post(
            "/api/v1/plugins/demo/execute",
            json={"account_id": "a1", "account_label": "Test", "settings": {"reward": 5}},
        )

    assert dashboard.status_code == 200
    assert '<dialog id="secret-dialog"' not in dashboard.text
    assert '<dialog id="delete-account-dialog"' in dashboard.text
    assert '<dialog id="browser-login-dialog"' in dashboard.text
    assert '<dialog id="execution-detail-dialog"' in dashboard.text
    assert '<dialog id="force-browser-save-dialog"' in dashboard.text
    assert '<dialog id="schedule-dialog"' in dashboard.text
    assert '<dialog id="channel-dialog"' in dashboard.text
    assert '<dialog id="channel-assignment-dialog"' in dashboard.text
    assert '<dialog id="delete-channel-dialog"' in dashboard.text
    assert 'id="channel-create"' in dashboard.text
    assert 'id="demo-test"' in dashboard.text
    assert 'id="history-clear"' in dashboard.text
    assert 'id="notification-channels"' in dashboard.text
    assert 'id="backup-summary"' in dashboard.text
    assert 'id="backup-run"' in dashboard.text
    assert 'id="backup-settings-dialog"' in dashboard.text
    assert 'id="backup-refresh"' not in dashboard.text
    assert "账户现已持久化到 SQLite" not in dashboard.text
    assert "可使用 Demo 验证流程" not in dashboard.text
    assert dashboard.text.index("<h2>账户</h2>") < dashboard.text.index("<h2>最近签到记录</h2>")
    assert dashboard.text.index("<h2>最近签到记录</h2>") < dashboard.text.index(
        "<h2>消息推送渠道</h2>"
    )
    assert dashboard.text.index("<h2>消息推送渠道</h2>") < dashboard.text.index(
        "<h2>系统备份</h2>"
    )
    assert 'id="browser-screenshot"' in dashboard.text
    assert 'id="browser-keyboard-capture"' in dashboard.text
    assert "browserFrameRequestActive" in dashboard.text
    assert 'id="execution-history"' in dashboard.text
    assert 'id="browser-text-form"' not in dashboard.text
    assert 'class="grid account-grid"' in dashboard.text
    assert ".account-grid { grid-template-columns: 1fr; margin-top: 16px; }" in dashboard.text
    assert "window.prompt(" not in dashboard.text
    assert "window.alert(" not in dashboard.text
    assert "window.confirm(" not in dashboard.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert plugins.status_code == 200
    assert {plugin["id"] for plugin in plugins.json()} == {
        "demo",
        "acgrip",
        "baidu_tieba",
        "yamibo",
    }
    assert execution.status_code == 200
    assert execution.json()["verified"] is True
    assert execution.json()["details"]["reward"] == 5


def test_backup_status_and_manual_actions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(backup, "KDF_N", 2**10)
    settings = settings_for_test(tmp_path)
    settings.backup_password = SecretStr("automatic backup password")
    with TestClient(create_app(settings)) as client:
        status = client.get("/api/v1/backups/status")
        updated = client.put(
            "/api/v1/backups/settings",
            json={
                "enabled": True,
                "daily_time": "04:45",
                "timezone": "UTC",
                "retention_count": 9,
            },
        )
        created = client.post("/api/v1/backups/run", json={})
        checked = client.post("/api/v1/backups/check-latest", json={})

    assert status.status_code == 200
    assert status.json()["configured"] is True
    assert status.json()["enabled"] is False
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    assert updated.json()["daily_time"] == "04:45"
    assert updated.json()["retention_count"] == 9
    assert created.status_code == 200
    assert created.json()["status"]["latest_backup_name"].startswith("autosign-auto-")
    assert checked.status_code == 200
    assert checked.json()["success"] is True


def test_unknown_plugin_returns_404(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for_test(tmp_path))) as client:
        response = client.post(
            "/api/v1/plugins/missing/execute",
            json={"account_id": "a1", "account_label": "Test"},
        )

    assert response.status_code == 404


def test_account_crud(tmp_path: Path) -> None:
    app = create_app(settings_for_test(tmp_path))
    notification_sender = AsyncMock(return_value=[])
    app.state.notifications.send_result = notification_sender
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "My Demo", "settings": {"reward": 2}},
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        execution = client.post(f"/api/v1/accounts/{account_id}/execute")
        assert execution.status_code == 200
        assert execution.json()["account_id"] == account_id
        notification_sender.assert_awaited_once()
        notification_call = notification_sender.await_args
        assert notification_call.args[0] == account_id
        assert notification_call.kwargs["account_label"] == "My Demo"
        assert notification_call.kwargs["plugin_id"] == "demo"
        history = client.get("/api/v1/executions")
        assert history.status_code == 200
        assert len(history.json()) == 1
        assert history.json()[0]["account_id"] == account_id
        assert history.json()[0]["account_label"] == "My Demo"
        assert history.json()[0]["status"] == "success"
        assert history.json()[0]["verified"] is True

        updated = client.patch(
            f"/api/v1/accounts/{account_id}",
            json={"enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert client.post(f"/api/v1/accounts/{account_id}/execute").status_code == 409

        wrong_confirmation = client.post(
            f"/api/v1/accounts/{account_id}/delete",
            json={"confirm_label": "Wrong label"},
        )
        assert wrong_confirmation.status_code == 400

        deleted = client.post(
            f"/api/v1/accounts/{account_id}/delete",
            json={"confirm_label": "My Demo"},
        )
        assert deleted.status_code == 204
        assert client.get("/api/v1/accounts").json() == []
        assert client.get("/api/v1/executions").json() == []


def test_account_schedule_crud(tmp_path: Path) -> None:
    with TestClient(create_app(settings_for_test(tmp_path))) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "Scheduled Demo"},
        ).json()
        account_id = account["id"]
        saved = client.put(
            f"/api/v1/accounts/{account_id}/schedule",
            json={
                "enabled": True,
                "daily_time": "08:30",
                "timezone": "Asia/Shanghai",
                "jitter_minutes": 20,
                "max_retries": 3,
                "retry_delay_minutes": 7,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["daily_time"] == "08:30"
        assert saved.json()["jitter_minutes"] == 20
        assert saved.json()["next_run_at"] is not None

        schedules = client.get("/api/v1/schedules").json()
        assert len(schedules) == 1
        assert schedules[0]["account_label"] == "Scheduled Demo"

        assert client.delete(f"/api/v1/accounts/{account_id}/schedule").status_code == 204
        assert client.get("/api/v1/schedules").json() == []


def test_uptime_channel_is_validated_encrypted_and_assignable(tmp_path: Path) -> None:
    app = create_app(settings_for_test(tmp_path))
    with TestClient(app) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "Kuma Demo"},
        ).json()
        account_id = account["id"]

        invalid = client.post(
            "/api/v1/notification-channels",
            json={
                "name": "Invalid Kuma",
                "channel_type": "uptime_kuma",
                "push_url": "https://kuma.example/not-a-push-token",
            },
        )
        assert invalid.status_code == 400

        push_url = "https://kuma.example/api/push/very-secret-token"
        saved = client.post(
            "/api/v1/notification-channels",
            json={
                "name": "VPS Kuma",
                "channel_type": "uptime_kuma",
                "push_url": push_url,
            },
        )
        assert saved.status_code == 201
        channel_id = saved.json()["id"]
        assigned = client.put(
            f"/api/v1/accounts/{account_id}/notification-channels",
            json={"channel_ids": [channel_id]},
        )
        assert assigned.status_code == 200
        assert assigned.json()[0]["id"] == channel_id
        refreshed = client.get(f"/api/v1/accounts/{account_id}").json()
        assert refreshed["monitor_configured"] is True
        assert refreshed["napcat_configured"] is False
        assert refreshed["notification_channel_ids"] == [channel_id]
        assert push_url not in (tmp_path / "autosign.db").read_bytes().decode(
            "utf-8",
            errors="ignore",
        )

        renamed = client.put(
            f"/api/v1/notification-channels/{channel_id}",
            json={"name": "Renamed Kuma", "channel_type": "uptime_kuma"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed Kuma"

        assert client.delete(f"/api/v1/notification-channels/{channel_id}").status_code == 204
        assert client.get(f"/api/v1/accounts/{account_id}").json()["monitor_configured"] is False


def test_napcat_channel_is_encrypted_reusable_and_unassignable(tmp_path: Path) -> None:
    app = create_app(settings_for_test(tmp_path))
    with TestClient(app) as client:
        first_account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "NapCat A"},
        ).json()
        second_account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "NapCat B"},
        ).json()
        saved = client.post(
            "/api/v1/notification-channels",
            json={
                "name": "VPS QQ",
                "channel_type": "napcat",
                "base_url": "http://napcat.example:3000",
                "access_token": "very-secret-napcat-token",
                "target_type": "group",
                "target_id": "987654321",
            },
        )
        assert saved.status_code == 201
        channel_id = saved.json()["id"]
        for account in (first_account, second_account):
            assigned = client.put(
                f"/api/v1/accounts/{account['id']}/notification-channels",
                json={"channel_ids": [channel_id]},
            )
            assert assigned.status_code == 200
            refreshed = client.get(f"/api/v1/accounts/{account['id']}").json()
            assert refreshed["napcat_configured"] is True

        listed = client.get("/api/v1/notification-channels").json()
        assert set(listed[0]["assigned_account_ids"]) == {
            first_account["id"],
            second_account["id"],
        }
        database_text = (tmp_path / "autosign.db").read_bytes().decode(
            "utf-8",
            errors="ignore",
        )
        assert "very-secret-napcat-token" not in database_text
        assert "987654321" not in database_text

        unassigned = client.put(
            f"/api/v1/accounts/{first_account['id']}/notification-channels",
            json={"channel_ids": []},
        )
        assert unassigned.status_code == 200
        assert unassigned.json() == []
        assert (
            client.get(f"/api/v1/accounts/{first_account['id']}").json()[
                "napcat_configured"
            ]
            is False
        )
        assert (
            client.get(f"/api/v1/accounts/{second_account['id']}").json()[
                "napcat_configured"
            ]
            is True
        )
