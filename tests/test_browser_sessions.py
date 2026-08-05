from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    PASSWORD_FORM_GUARD_SCRIPT,
    SESSION_STORAGE_CAPTURE_SCRIPT,
    SESSION_STORAGE_STATE_KEY,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    BrowserSessionCleanupCoordinator,
    BrowserSessionInfo,
    BrowserSessionManager,
    BrowserSessionNotFoundError,
    BrowserStorageStateError,
    PlaywrightAutomationClient,
    normalize_storage_state,
    session_storage_restore_script,
    unpack_storage_state,
)
from autosign.core.config import Settings
from autosign.core.security import SecretCipher
from autosign.web.app import create_app


class FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.pressed: list[str] = []

    async def insert_text(self, text: str) -> None:
        self.inserted.append(text)

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeLocator:
    def __init__(self) -> None:
        self.clicked = False
        self.click_error: Exception | None = None
        self.dom_clicked = False

    @property
    def first(self):
        return self

    def nth(self, _index: int):
        return self

    async def is_visible(self, **_kwargs) -> bool:
        return True

    async def count(self) -> int:
        return 1

    async def click(self, **_kwargs) -> None:
        if self.click_error is not None:
            raise self.click_error
        self.clicked = True

    async def evaluate(self, _script: str) -> bool:
        self.dom_clicked = True
        return True


class FakePage:
    def __init__(self, *, title: str = "Fake Login") -> None:
        self.url = "about:blank"
        self.page_title = title
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.closed = False
        self.screenshot_error: Exception | None = None
        self.fake_locator = FakeLocator()
        self.event_handlers: dict[str, list] = {}
        self.brought_to_front = False
        self.session_storage_entries: list[list[str]] = []

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def title(self) -> str:
        return self.page_title

    async def screenshot(self, **_kwargs) -> bytes:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return b"fake-png"

    async def bring_to_front(self) -> None:
        self.brought_to_front = True

    async def evaluate(self, script: str) -> dict:
        if script == SESSION_STORAGE_CAPTURE_SCRIPT:
            return {
                "origin": "https://example.test",
                "entries": self.session_storage_entries,
            }
        return {"origin": "https://example.test", "databases": []}

    def locator(self, _selector: str) -> FakeLocator:
        return self.fake_locator

    def is_closed(self) -> bool:
        return self.closed

    def on(self, event: str, callback) -> None:
        self.event_handlers.setdefault(event, []).append(callback)

    def emit_close(self) -> None:
        self.closed = True
        for callback in self.event_handlers.get("close", []):
            callback()


class FakeCdpSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.commands: list[tuple[str, dict]] = []
        self.detached = False

    async def send(self, method: str, params: dict) -> dict[str, str]:
        self.commands.append((method, params))
        if self.page.screenshot_error is not None:
            raise self.page.screenshot_error
        return {"data": base64.b64encode(b"fake-png").decode("ascii")}

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.page.context = self
        self.pages = [self.page]
        self.closed = False
        self.event_handlers: dict[str, list] = {}
        self.init_scripts: list[str] = []
        self.storage_state_indexed_db: bool | None = None
        self.cdp_sessions: list[FakeCdpSession] = []

    async def add_init_script(self, *, script: str) -> None:
        self.init_scripts.append(script)

    async def new_page(self) -> FakePage:
        return self.page

    async def new_cdp_session(self, page: FakePage) -> FakeCdpSession:
        session = FakeCdpSession(page)
        self.cdp_sessions.append(session)
        return session

    def on(self, event: str, callback) -> None:
        self.event_handlers.setdefault(event, []).append(callback)

    def emit_page(self, page: FakePage) -> None:
        self.pages.append(page)
        for callback in self.event_handlers.get("page", []):
            callback(page)

    async def storage_state(self, *, indexed_db: bool = False) -> dict:
        self.storage_state_indexed_db = indexed_db
        origins = []
        if indexed_db:
            origins.append(
                {
                    "origin": "https://example.test",
                    "localStorage": [],
                    "indexedDB": [{"name": "auth", "data": "encrypted-later"}],
                }
            )
        return {
            "cookies": [{"name": "session", "value": "encrypted-later"}],
            "origins": origins,
        }

    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "forum_auth", "value": "not-printed"}]

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_automation_client_clicks_plugin_selector() -> None:
    page = FakePage()
    client = PlaywrightAutomationClient(page)

    assert await client.click('button:has-text("Sign")') is True
    assert page.fake_locator.clicked is True


@pytest.mark.asyncio
async def test_automation_client_uses_dom_click_after_actionability_timeout() -> None:
    page = FakePage()
    page.fake_locator.click_error = TimeoutError("covered by custom page layer")
    client = PlaywrightAutomationClient(page)

    assert await client.click('text="立即签到"') is True
    assert page.fake_locator.clicked is False
    assert page.fake_locator.dom_clicked is True


def test_normalize_storage_state_repairs_falsey_indexeddb_keys() -> None:
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://www.vikacg.com",
                "localStorage": [],
                "indexedDB": [
                    {
                        "name": "auth",
                        "version": 1,
                        "stores": [
                            {
                                "name": "tokens",
                                "autoIncrement": False,
                                "records": [
                                    {"value": {"kind": "zero"}},
                                    {"value": {"kind": "empty"}},
                                    {"key": "auth", "value": "token"},
                                ],
                                "indexes": [],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    normalized, repaired = normalize_storage_state(state)
    records = normalized["origins"][0]["indexedDB"][0]["stores"][0]["records"]

    assert repaired == 2
    assert [record.get("key") for record in records] == [0, "", "auth"]


def test_exact_falsey_indexeddb_keys_override_fallbacks() -> None:
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://www.vikacg.com",
                "indexedDB": [
                    {
                        "name": "auth",
                        "stores": [
                            {
                                "name": "tokens",
                                "records": [{"value": "empty-key-token"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    key_maps = [
        {
            "origin": "https://www.vikacg.com",
            "databases": [
                {
                    "name": "auth",
                    "stores": [
                        {"name": "tokens", "records": [{"index": 0, "key": ""}]}
                    ],
                }
            ],
        }
    ]

    BrowserSessionManager._apply_falsey_indexeddb_keys(state, key_maps)

    record = state["origins"][0]["indexedDB"][0]["stores"][0]["records"][0]
    assert record["key"] == ""


def test_unpack_storage_state_separates_session_storage_extension() -> None:
    state = {
        "cookies": [],
        "origins": [],
        SESSION_STORAGE_STATE_KEY: [
            {
                "origin": "https://www.vikacg.com",
                "entries": [["auth", "test-only-token"]],
            }
        ],
    }

    playwright_state, session_storage = unpack_storage_state(state)
    script = session_storage_restore_script(session_storage)

    assert SESSION_STORAGE_STATE_KEY not in playwright_state
    assert session_storage[0]["origin"] == "https://www.vikacg.com"
    assert script is not None
    assert "sessionStorage.setItem" in script
    assert "test-only-token" in script


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.received_storage_state = None
        self.received_context_options: list[dict] = []
        self.closed = False

    async def new_context(self, *, storage_state=None, **kwargs) -> FakeContext:
        self.received_storage_state = storage_state
        self.received_context_options.append(kwargs)
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed


class StorageRestoreFailingBrowser(FakeBrowser):
    def __init__(self, *, always_fail: bool = False) -> None:
        super().__init__()
        self.always_fail = always_fail

    async def new_context(self, *, storage_state=None, **kwargs) -> FakeContext:
        if storage_state is not None or self.always_fail:
            raise RuntimeError(
                "Browser.new_context: Error setting storage state: "
                "Unable to restore IndexedDB"
            )
        return await super().new_context(storage_state=storage_state, **kwargs)


def test_browser_manager_can_hide_headful_window_offscreen() -> None:
    manager = BrowserSessionManager(headless=False, hide_window=True)

    assert manager._headless is False
    assert "--window-position=-32000,-32000" in manager._launch_args
    assert f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}" in manager._launch_args


@pytest.mark.asyncio
async def test_browser_manager_applies_proxy_and_password_form_guard() -> None:
    manager = BrowserSessionManager(
        proxy_server="http://proxy.example:7890",
        proxy_bypass="login.example.test",
    )
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-proxy",
        login_url="https://login.example.test/sign",
    )

    assert fake_browser.received_context_options[0]["proxy"] == {
        "server": "http://proxy.example:7890",
        "bypass": "login.example.test",
    }
    assert fake_browser.contexts[0].init_scripts == [PASSWORD_FORM_GUARD_SCRIPT]

    await manager.close(info.id, save_state=False)


@pytest.mark.asyncio
async def test_automation_context_reuses_proxy_and_form_guard() -> None:
    manager = BrowserSessionManager(proxy_server="socks5://proxy.example:1080")
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    async with manager.automation(
        storage_state_json='{"cookies":[],"origins":[]}',
    ):
        pass

    assert fake_browser.received_context_options[0]["proxy"] == {
        "server": "socks5://proxy.example:1080",
    }
    assert fake_browser.contexts[0].init_scripts == [PASSWORD_FORM_GUARD_SCRIPT]
    assert fake_browser.contexts[0].closed is True


@pytest.mark.asyncio
async def test_automation_restores_encrypted_session_storage_extension() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser
    state = {
        "cookies": [],
        "origins": [],
        SESSION_STORAGE_STATE_KEY: [
            {
                "origin": "https://www.vikacg.com",
                "entries": [["auth", "test-only-token"]],
            }
        ],
    }

    async with manager.automation(storage_state_json=json.dumps(state)):
        pass

    scripts = fake_browser.contexts[0].init_scripts
    assert "sessionStorage.setItem" in scripts[0]
    assert scripts[1] == PASSWORD_FORM_GUARD_SCRIPT


@pytest.mark.asyncio
async def test_automation_state_capture_includes_refreshed_browser_storage() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    async with manager.automation(
        storage_state_json='{"cookies":[],"origins":[]}',
    ) as client:
        client.page.session_storage_entries = [["auth", "refreshed-token"]]
        state_json = await manager.capture_automation_state(client)

    state = json.loads(state_json)
    assert state["cookies"][0]["name"] == "session"
    assert state[SESSION_STORAGE_STATE_KEY] == [
        {
            "origin": "https://example.test",
            "entries": [["auth", "refreshed-token"]],
        }
    ]
    assert fake_browser.contexts[0].storage_state_indexed_db is True


@pytest.mark.asyncio
async def test_browser_manager_saves_session_storage_extension() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-session-storage",
        login_url="https://example.test/login",
    )
    fake_browser.contexts[0].page.session_storage_entries = [
        ["auth", "test-only-token"]
    ]
    state_json = await manager.close(info.id, save_state=True)
    saved_state = json.loads(state_json)

    assert saved_state[SESSION_STORAGE_STATE_KEY] == [
        {
            "origin": "https://example.test",
            "entries": [["auth", "test-only-token"]],
        }
    ]


@pytest.mark.asyncio
async def test_interactive_login_falls_back_to_clean_state_after_restore_error() -> None:
    manager = BrowserSessionManager()
    fake_browser = StorageRestoreFailingBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-broken-state",
        login_url="https://example.test/login",
        storage_state_json='{"cookies":[{"name":"session","value":"old"}],"origins":[]}',
    )

    assert info.url == "https://example.test/login"
    assert len(fake_browser.contexts) == 1
    await manager.close(info.id, save_state=False)


@pytest.mark.asyncio
async def test_automation_reports_storage_restore_error_clearly() -> None:
    manager = BrowserSessionManager()
    manager._browser = StorageRestoreFailingBrowser(always_fail=True)

    with pytest.raises(BrowserStorageStateError, match="interactive login"):
        async with manager.automation(
            storage_state_json='{"cookies":[],"origins":[]}',
        ):
            pass


@pytest.mark.asyncio
async def test_browser_manager_lifecycle_and_input() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-1",
        login_url="https://example.test/login",
    )
    assert info.url == "https://example.test/login"
    assert await manager.screenshot(info.id) == b"fake-png"
    cdp_session = fake_browser.contexts[0].cdp_sessions[0]
    assert cdp_session.commands == [
        (
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
            },
        )
    ]
    assert cdp_session.detached is True
    await manager.focus(info.id)

    await manager.click(info.id, x=100, y=200)
    pasted_text = "用户+密碼'\" <token>&"
    await manager.type_text(info.id, text=pasted_text)
    await manager.press_key(info.id, key="Enter")
    assert await manager.login_is_complete(info.id, selectors=("#logged-in",)) is True
    assert await manager.login_is_complete(
        info.id,
        selectors=(),
        cookie_name_suffixes=("_auth",),
    ) is True

    page = fake_browser.contexts[0].page
    assert page.brought_to_front is True
    assert page.mouse.clicks == [(100, 200)]
    assert page.keyboard.inserted == [pasted_text]
    assert page.keyboard.pressed == ["Enter"]

    state_json = await manager.close(info.id, save_state=True)
    saved_state = json.loads(state_json)
    assert saved_state["cookies"][0]["name"] == "session"
    assert saved_state["origins"][0]["indexedDB"][0]["name"] == "auth"
    assert fake_browser.contexts[0].storage_state_indexed_db is True
    assert fake_browser.contexts[0].closed is True
    with pytest.raises(BrowserSessionNotFoundError):
        await manager.get_info(info.id)


@pytest.mark.asyncio
async def test_browser_manager_closes_idle_session_without_new_request() -> None:
    manager = BrowserSessionManager(timeout_seconds=10)
    fake_browser = FakeBrowser()
    manager._browser = fake_browser
    info = await manager.start(
        account_id="account-expired",
        login_url="https://example.test/login",
    )
    manager._sessions[info.id].last_activity = datetime.now(UTC) - timedelta(seconds=11)

    assert await manager.cleanup_expired() == 1
    assert fake_browser.contexts[0].closed is True
    assert info.id not in manager._sessions
    assert "account-expired" not in manager._account_sessions
    assert await manager.cleanup_expired() == 0


@pytest.mark.asyncio
async def test_browser_cleanup_coordinator_runs_and_stops() -> None:
    class CleanupManager:
        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_expired(self) -> int:
            self.calls += 1
            return 1 if self.calls == 1 else 0

    manager = CleanupManager()
    coordinator = BrowserSessionCleanupCoordinator(manager, poll_seconds=0.01)  # type: ignore[arg-type]
    coordinator.start()
    await asyncio.sleep(0.025)
    await coordinator.stop()

    assert manager.calls >= 2


@pytest.mark.asyncio
async def test_browser_manager_follows_popup_and_restores_opener() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-popup",
        login_url="https://example.test/login",
    )
    context = fake_browser.contexts[0]
    opener = context.page
    popup = FakePage(title="Security verification")
    popup.url = "https://verify.example.test/challenge"

    context.emit_page(popup)
    popup_info = await manager.get_info(info.id)
    assert popup_info.url == "https://verify.example.test/challenge"
    assert popup_info.title == "Security verification"

    popup.emit_close()
    restored_info = await manager.get_info(info.id)
    assert restored_info.url == opener.url
    assert restored_info.title == "Fake Login"


@pytest.mark.asyncio
async def test_closed_browser_target_is_discarded_with_reopen_message() -> None:
    manager = BrowserSessionManager()
    fake_browser = FakeBrowser()
    manager._browser = fake_browser

    info = await manager.start(
        account_id="account-closed",
        login_url="https://example.test/login",
    )
    target_error = type(
        "TargetClosedError",
        (Exception,),
        {},
    )("Target page, context or browser has been closed")
    fake_browser.contexts[0].page.screenshot_error = target_error

    with pytest.raises(BrowserSessionNotFoundError, match="Reopen interactive login"):
        await manager.screenshot(info.id)
    with pytest.raises(BrowserSessionNotFoundError):
        await manager.get_info(info.id)


class FakeApiBrowserManager:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.info = BrowserSessionInfo(
            id="browser-session-1",
            account_id="",
            url="http://testserver/demo-login",
            title="Fake Login",
            created_at=now,
            last_activity=now,
        )
        self.closed = False
        self.login_complete = True
        self.started_storage_states: list[str | None] = []

    async def start(
        self,
        *,
        account_id: str,
        storage_state_json: str | None = None,
        **_kwargs,
    ) -> BrowserSessionInfo:
        self.started_storage_states.append(storage_state_json)
        self.info = BrowserSessionInfo(
            id=self.info.id,
            account_id=account_id,
            url=self.info.url,
            title=self.info.title,
            created_at=self.info.created_at,
            last_activity=self.info.last_activity,
        )
        return self.info

    async def get_info(self, _session_id: str) -> BrowserSessionInfo:
        return self.info

    async def screenshot(self, _session_id: str) -> bytes:
        return b"fake-png"

    async def click(self, _session_id: str, **_kwargs) -> None:
        pass

    async def type_text(self, _session_id: str, **_kwargs) -> None:
        pass

    async def press_key(self, _session_id: str, **_kwargs) -> None:
        pass

    async def login_is_complete(self, _session_id: str, **_kwargs) -> bool:
        return self.login_complete

    async def close(self, _session_id: str, *, save_state: bool) -> str | None:
        self.closed = True
        if save_state:
            return '{"cookies":[{"name":"demo","value":"state"}],"origins":[]}'
        return None

    async def close_all(self) -> None:
        self.closed = True


def test_browser_api_saves_state_in_account_vault(tmp_path: Path) -> None:
    settings = Settings(
        environment="testing",
        data_dir=tmp_path,
        master_key=SecretStr(SecretCipher.generate_key()),
        auth_disabled=True,
    )
    browser_manager = FakeApiBrowserManager()
    app = create_app(settings, browser_manager_override=browser_manager)

    with TestClient(app) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "浏览器测试"},
        ).json()
        started = client.post(f"/api/v1/accounts/{account['id']}/browser-session")
        assert started.status_code == 200
        session_id = started.json()["id"]

        screenshot = client.get(f"/api/v1/browser-sessions/{session_id}/screenshot")
        assert screenshot.content == b"fake-png"
        assert screenshot.headers["cache-control"] == "no-store, max-age=0"

        closed = client.post(
            f"/api/v1/browser-sessions/{session_id}/close",
            json={"save_state": True},
        )
        assert closed.status_code == 200
        assert closed.json()["verified"] is True
        assert BROWSER_STATE_SECRET in closed.json()["secret_names"]
        stored = client.app.state.vault.get(account["id"], BROWSER_STATE_SECRET)
        assert '"value":"state"' in stored

        restored = client.post(f"/api/v1/accounts/{account['id']}/browser-session")
        assert restored.status_code == 200
        assert browser_manager.started_storage_states[-1] == stored
        client.post(
            f"/api/v1/browser-sessions/{restored.json()['id']}/close",
            json={"save_state": False},
        )

        clean = client.post(
            f"/api/v1/accounts/{account['id']}/browser-session?clean=true"
        )
        assert clean.status_code == 200
        assert browser_manager.started_storage_states[-1] is None
        client.post(
            f"/api/v1/browser-sessions/{clean.json()['id']}/close",
            json={"save_state": False},
        )

        browser_manager.login_complete = False
        second_account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "yamibo", "label": "强制保存测试"},
        ).json()
        second_session = client.post(
            f"/api/v1/accounts/{second_account['id']}/browser-session"
        ).json()["id"]
        not_detected = client.post(
            f"/api/v1/browser-sessions/{second_session}/close",
            json={"save_state": True},
        )
        assert not_detected.status_code == 409
        forced = client.post(
            f"/api/v1/browser-sessions/{second_session}/close",
            json={"save_state": True, "force_save": True},
        )
        assert forced.status_code == 200
        assert forced.json()["saved"] is True
        assert forced.json()["verified"] is False
        assert BROWSER_STATE_SECRET in forced.json()["secret_names"]
