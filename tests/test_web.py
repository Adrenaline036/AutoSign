import json
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign import __version__
from autosign.core import backup
from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    DeferredChromeBrowserSessionManager,
)
from autosign.core.config import Settings
from autosign.core.security import SecretCipher
from autosign.plugins.vikacg import VikacgImportError, VikacgImportResult, VikacgPlugin
from autosign.web.app import create_app


class FakeImportBrowserManager:
    def __init__(self) -> None:
        self.candidate_state = ""

    @asynccontextmanager
    async def automation(self, *, storage_state_json: str):
        self.candidate_state = storage_state_json
        yield object()

    async def capture_automation_state(self, _browser: object) -> str:
        return self.candidate_state

    async def cleanup_expired(self) -> int:
        return 0

    async def close_all(self) -> None:
        return None


def vikacg_browser_state(token: str = "old-token") -> str:
    return json.dumps(
        {
            "cookies": [],
            "origins": [
                {
                    "origin": VikacgPlugin.ORIGIN,
                    "localStorage": [],
                    "indexedDB": [
                        {
                            "name": "localforage",
                            "version": 1,
                            "stores": [
                                {
                                    "name": "keyvaluepairs",
                                    "autoIncrement": False,
                                    "keyPath": None,
                                    "records": [
                                        {
                                            "key": VikacgPlugin.ACCOUNT_STORAGE_KEY,
                                            "value": json.dumps(
                                                {
                                                    "accounts": [
                                                        {
                                                            "id": 42,
                                                            "token": token,
                                                            "refreshToken": "old-refresh",
                                                        }
                                                    ],
                                                    "currentID": 42,
                                                }
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


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
        system_status = client.get("/api/v1/system/status")
        execution = client.post(
            "/api/v1/plugins/demo/execute",
            json={"account_id": "a1", "account_label": "Test", "settings": {"reward": 5}},
        )

    assert dashboard.status_code == 200
    assert '<dialog id="secret-dialog"' not in dashboard.text
    assert '<dialog id="delete-account-dialog"' in dashboard.text
    assert '<dialog id="browser-login-dialog"' in dashboard.text
    assert '<dialog id="vikacg-recovery-dialog"' in dashboard.text
    assert 'id="vikacg-import-value" class="secret-json-input" maxlength="65536"' in dashboard.text
    assert 'type="password" autocomplete="off"' in dashboard.text
    assert 'isVikacg ? "登录与恢复" : "交互登录"' in dashboard.text
    assert "/vikacg-state-import" in dashboard.text
    assert '<dialog id="execution-detail-dialog"' in dashboard.text
    assert '<dialog id="force-browser-save-dialog"' in dashboard.text
    assert '<dialog id="schedule-dialog"' in dashboard.text
    assert '<dialog id="channel-dialog"' in dashboard.text
    assert '<dialog id="channel-assignment-dialog"' in dashboard.text
    assert 'class="browser-dialog"' in dashboard.text
    assert 'id="channel-assignment-list" class="channel-assignment-columns"' in dashboard.text
    assert '.channel-assignment-columns { display: grid;' in dashboard.text
    assert 'element("section", "channel-assignment-column")' in dashboard.text
    assert '<dialog id="delete-channel-dialog"' in dashboard.text
    assert 'id="channel-create"' in dashboard.text
    assert 'id="demo-test"' in dashboard.text
    assert 'id="history-clear"' in dashboard.text
    assert 'id="notification-channels"' in dashboard.text
    assert 'id="backup-summary"' in dashboard.text
    assert 'id="backup-run"' in dashboard.text
    assert 'id="backup-settings-dialog"' in dashboard.text
    assert 'id="system-status"' in dashboard.text
    assert "/api/v1/system/status" in dashboard.text
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
    assert '<dialog id="vikacg-import-dialog"' in dashboard.text
    assert '.recovery-options { display: grid; grid-template-columns: 1fr;' in dashboard.text
    assert '>尝试导入 accountStore3</button>' in dashboard.text
    assert "不要选择 Cookies" in dashboard.text
    assert "本地存储空间 / Local Storage" in dashboard.text
    assert "当前网页版通常保存在 Local Storage" in dashboard.text
    assert "localforage → keyvaluepairs" in dashboard.text
    assert "accountStore3 是右侧记录表中的 Key" in dashboard.text
    assert 'id="browser-screenshot"' not in dashboard.text
    assert 'id="browser-live-panel"' in dashboard.text
    assert 'id="browser-live-open"' in dashboard.text
    assert 'id="browser-native-help"' in dashboard.text
    assert "普通 Chrome 登录窗口已打开（尚未接管）" in dashboard.text
    assert "AutoSign 此时尚未连接浏览器" in dashboard.text
    assert "/focus`" in dashboard.text
    assert "activeBrowserSession.live_url" in dashboard.text
    assert 'id="browser-keyboard-capture"' not in dashboard.text
    assert "无法连接 AutoSign 服务" in dashboard.text
    assert 'id="execution-history"' in dashboard.text
    assert 'id="browser-text-form"' not in dashboard.text
    assert 'class="grid account-grid"' in dashboard.text
    assert ".account-grid { grid-template-columns: 1fr; margin-top: 16px; }" in dashboard.text
    assert "window.prompt(" not in dashboard.text
    assert "window.alert(" not in dashboard.text
    assert "window.confirm(" not in dashboard.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert system_status.status_code == 200
    assert system_status.json()["browser_capacity"] == {
        "automation": {"limit": 2, "active": 0, "waiting": 0},
        "interactive": {"limit": 1, "active": 0, "waiting": 0},
        "closing": False,
    }
    assert "account" not in json.dumps(system_status.json()).lower()
    assert plugins.status_code == 200
    assert {plugin["id"] for plugin in plugins.json()} == {
        "demo",
        "acgrip",
        "baidu_tieba",
        "vikacg",
        "yamibo",
    }
    assert execution.status_code == 200
    assert execution.json()["verified"] is True
    assert execution.json()["details"]["reward"] == 5


def test_source_and_health_versions_are_consistent(tmp_path: Path) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    with TestClient(create_app(settings_for_test(tmp_path))) as client:
        health_version = client.get("/healthz").json()["version"]

    assert project["project"]["version"] == __version__ == health_version


def test_local_preview_remote_browser_is_a_separate_interactive_page() -> None:
    remote_browser = (
        Path(__file__).parents[1]
        / "src"
        / "autosign"
        / "web"
        / "static"
        / "remote_browser.html"
    ).read_text(encoding="utf-8")

    assert "AutoSign 独立登录浏览器" in remote_browser
    assert 'id="screen"' in remote_browser
    assert 'id="keyboard" type="text"' in remote_browser
    assert 'id="paste-input" type="password"' in remote_browser
    assert 'id="paste-send"' in remote_browser
    assert "remoteInputArmed = true" in remote_browser
    assert "stage.focus({preventScroll: true})" not in remote_browser
    assert "/screenshot?t=${Date.now()}" in remote_browser
    assert "/click`" in remote_browser
    assert "/type`" in remote_browser
    assert "/press`" in remote_browser
    assert "window.prompt(" not in remote_browser
    assert "window.alert(" not in remote_browser
    assert "window.confirm(" not in remote_browser


def test_native_executable_selects_deferred_chrome_manager(tmp_path: Path) -> None:
    settings = settings_for_test(tmp_path)
    settings.browser_native_window = True
    settings.browser_native_executable = tmp_path / "chrome.exe"

    app = create_app(settings)

    assert isinstance(
        app.state.browser_sessions,
        DeferredChromeBrowserSessionManager,
    )


def test_docker_nas_uses_deferred_chromium_for_interactive_login() -> None:
    project_root = Path(__file__).parents[1]
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    for compose_name in (
        "compose.yaml",
        "compose.nas.yaml",
        "compose.nas.bootstrap.yaml",
    ):
        compose = (project_root / compose_name).read_text(encoding="utf-8")
        assert (
            "AUTOSIGN_BROWSER_NATIVE_EXECUTABLE: /usr/local/bin/autosign-browser"
            in compose
        )

    assert "ln -s \"$browser_executable\" /usr/local/bin/autosign-browser" in dockerfile


def test_vikacg_state_import_requires_confirmation_and_preserves_old_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = FakeImportBrowserManager()
    app = create_app(settings_for_test(tmp_path), browser_manager_override=manager)
    with TestClient(app) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "vikacg", "label": "VikACG test"},
        ).json()
        old_state = vikacg_browser_state()
        client.app.state.vault.set(account["id"], BROWSER_STATE_SECRET, old_state)
        imported = json.dumps(
            {"accounts": [{"id": 42, "token": "new-token"}], "currentID": 42}
        )

        confirmation = client.post(
            f"/api/v1/accounts/{account['id']}/vikacg-state-import",
            json={"raw_json": imported, "confirm_overwrite": False},
        )
        assert confirmation.status_code == 409
        assert client.app.state.vault.get(account["id"], BROWSER_STATE_SECRET) == old_state

        async def reject_import(_self, _browser, *, force_refresh=False):
            raise VikacgImportError("导入状态无效。")

        monkeypatch.setattr(VikacgPlugin, "validate_imported_session", reject_import)
        rejected = client.post(
            f"/api/v1/accounts/{account['id']}/vikacg-state-import",
            json={"raw_json": imported, "confirm_overwrite": True},
        )
        assert rejected.status_code == 400
        assert client.app.state.vault.get(account["id"], BROWSER_STATE_SECRET) == old_state


def test_vikacg_state_import_saves_only_after_validation(tmp_path: Path, monkeypatch) -> None:
    manager = FakeImportBrowserManager()
    app = create_app(settings_for_test(tmp_path), browser_manager_override=manager)

    async def accept_import(_self, _browser, *, force_refresh=False):
        return VikacgImportResult(True, True, False)

    monkeypatch.setattr(VikacgPlugin, "validate_imported_session", accept_import)
    with TestClient(app) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "vikacg", "label": "VikACG test"},
        ).json()
        client.app.state.vault.set(account["id"], BROWSER_STATE_SECRET, vikacg_browser_state())
        imported = json.dumps(
            {
                "accounts": [
                    {"id": 42, "token": "new-token", "refreshToken": "new-refresh"}
                ],
                "currentID": 42,
            }
        )
        response = client.post(
            f"/api/v1/accounts/{account['id']}/vikacg-state-import",
            json={"raw_json": imported, "confirm_overwrite": True},
        )

        assert response.status_code == 200
        assert response.json() == {
            "imported": True,
            "token": True,
            "refresh_token": True,
            "token_refreshed": False,
            "device_profile_preserved": True,
        }
        saved = client.app.state.vault.get(account["id"], BROWSER_STATE_SECRET)
        records = json.loads(saved)["origins"][0]["indexedDB"][0]["stores"][0]["records"]
        account_cache = json.loads(records[0]["value"])
        assert account_cache["accounts"][0]["token"] == "new-token"
        assert account_cache["accounts"][0]["refreshToken"] == "new-refresh"


def test_live_browser_uses_vnc_clipboard_with_balanced_modifiers() -> None:
    live_browser = (
        Path(__file__).parents[1]
        / "src"
        / "autosign"
        / "web"
        / "static"
        / "live_browser.html"
    ).read_text(encoding="utf-8")

    assert 'id="paste-input" type="password"' in live_browser
    assert 'maxlength="4096" autocomplete="off"' in live_browser
    assert 'fetch("/api/v1/auth/status"' in live_browser
    assert '"X-AutoSign-CSRF": token' in live_browser
    assert "/api/v1/browser-sessions/${encodeURIComponent(sessionId)}/type" not in live_browser
    assert "rfb.clipboardPasteFrom(text);" in live_browser
    assert 'rfb.sendKey(0xffe3, "ControlLeft", true);' in live_browser
    assert 'rfb.sendKey(0x0076, "KeyV", true);' in live_browser
    assert 'rfb.sendKey(0x0076, "KeyV", false);' in live_browser
    assert 'rfb.sendKey(0xffe3, "ControlLeft", false);' in live_browser
    assert "function releaseRemoteModifiers()" in live_browser
    assert 'window.addEventListener("blur", releaseRemoteModifiers);' in live_browser
    assert 'window.addEventListener("pagehide", releaseRemoteModifiers);' in live_browser
    assert 'document.addEventListener("visibilitychange"' in live_browser
    assert live_browser.count("rfb.focus();") == 2
    assert 'document.addEventListener("paste"' in live_browser
    assert "event.stopImmediatePropagation()" in live_browser
    assert "status.textContent = `已向远端输入框发送 ${text.length} 个字符。`;" in live_browser
    assert "/api/v1/browser-sessions/${encodeURIComponent(sessionId)}/activity" in live_browser
    assert 'screen.addEventListener("pointerdown"' in live_browser
    assert 'screen.addEventListener("wheel"' in live_browser
    assert "void reportRemoteActivity();" in live_browser


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
