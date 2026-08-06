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
    DeferredChromeBrowserSessionManager,
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
        self.storage_write: dict | None = None
        self.reload_calls = 0

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def reload(self, **_kwargs) -> None:
        self.reload_calls += 1

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        pass

    async def title(self) -> str:
        return self.page_title

    async def screenshot(self, **_kwargs) -> bytes:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return b"fake-png"

    async def bring_to_front(self) -> None:
        self.brought_to_front = True

    async def evaluate(self, script: str, arg=None):
        if isinstance(arg, dict) and {"key", "value"} <= arg.keys():
            self.storage_write = arg
            return True
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
        self.indexed_state: dict | None = None
        self.cdp_sessions: list[FakeCdpSession] = []
        self.request = FakeApiRequest()

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
        if indexed_db and self.indexed_state is not None:
            return self.indexed_state
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


class FakeApiResponse:
    status = 200
    url = "https://example.test/api"

    async def text(self) -> str:
        return '{"status":"success"}'


class FakeApiRequest:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> FakeApiResponse:
        self.posts.append((url, kwargs))
        return FakeApiResponse()


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


@pytest.mark.asyncio
async def test_automation_client_posts_json_through_context_request() -> None:
    context = FakeContext()
    client = PlaywrightAutomationClient(context.page)

    response = await client.post_json(
        "https://example.test/api",
        {"action": "sign"},
        headers={"Authorization": "Bearer test-only-token"},
    )

    assert response.status == 200
    assert response.text == '{"status":"success"}'
    assert context.request.posts == [
        (
            "https://example.test/api",
            {
                "data": {"action": "sign"},
                "headers": {"Authorization": "Bearer test-only-token"},
                "timeout": 30_000,
            },
        )
    ]


@pytest.mark.asyncio
async def test_automation_client_reads_and_writes_indexed_db_value() -> None:
    context = FakeContext()
    context.indexed_state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://example.test",
                "indexedDB": [
                    {
                        "name": "localforage",
                        "stores": [
                            {
                                "name": "keyvaluepairs",
                                "records": [
                                    {"key": "accountStore3", "value": "saved-state"}
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    client = PlaywrightAutomationClient(context.page)

    assert (
        await client.storage_value("https://example.test", "accountStore3")
        == "saved-state"
    )
    assert await client.write_storage_value("accountStore3", "updated-state") is True
    assert context.page.storage_write == {
        "key": "accountStore3",
        "value": "updated-state",
    }


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


class FakeNativeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class FakeCdpChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connect_calls: list[tuple[str, int]] = []

    async def connect_over_cdp(self, url: str, **kwargs) -> FakeBrowser:
        self.connect_calls.append((url, kwargs["timeout"]))
        return self.browser


class FakeDeferredPlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeCdpChromium(browser)


async def _start_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_server: str | None = None,
    proxy_bypass: str | None = None,
) -> tuple[
    DeferredChromeBrowserSessionManager,
    BrowserSessionInfo,
    FakeNativeProcess,
    list[tuple],
]:
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)
    executable = tmp_path / "chrome.exe"
    await asyncio.to_thread(executable.touch)
    process = FakeNativeProcess()
    launch_calls: list[tuple] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        launch_calls.append((*args, kwargs))
        return process

    async def debug_port_ready(_process, _port: int) -> None:
        pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    manager = DeferredChromeBrowserSessionManager(
        executable_path=executable,
        profile_root=tmp_path / "profiles",
        timeout_seconds=10,
        proxy_server=proxy_server,
        proxy_bypass=proxy_bypass,
    )
    monkeypatch.setattr(manager, "_wait_for_debug_port", debug_port_ready)
    info = await manager.start(
        account_id="native-account",
        login_url="https://example.test/login",
    )
    return manager, info, process, launch_calls


@pytest.mark.asyncio
async def test_deferred_chrome_launches_without_playwright_automation_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, info, process, launch_calls = await _start_native_session(
        tmp_path, monkeypatch
    )

    arguments = [str(value) for value in launch_calls[0][:-1]]
    assert any(value.startswith("--user-data-dir=") for value in arguments)
    assert any(value.startswith("--remote-debugging-port=") for value in arguments)
    assert "--remote-debugging-address=127.0.0.1" in arguments
    assert not any("enable-automation" in value for value in arguments)
    assert manager._native_sessions[info.id].browser is None

    manager._native_sessions[info.id].last_activity = datetime.now(UTC) - timedelta(
        seconds=5
    )
    previous_activity = manager._native_sessions[info.id].last_activity
    await manager.mark_activity(info.id)
    assert manager._native_sessions[info.id].last_activity > previous_activity

    profile_dir = manager._native_sessions[info.id].profile_dir
    await manager.close(info.id, save_state=False)
    assert process.terminated is True
    assert not profile_dir.exists()
    assert info.id not in manager._native_sessions
    assert "native-account" not in manager._account_sessions


@pytest.mark.asyncio
async def test_deferred_chrome_passes_proxy_without_playwright_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, info, _process, launch_calls = await _start_native_session(
        tmp_path,
        monkeypatch,
        proxy_server="http://proxy.example:7890",
        proxy_bypass="www.vikacg.com,bbs.yamibo.com",
    )

    arguments = [str(value) for value in launch_calls[0][:-1]]
    assert "--proxy-server=http://proxy.example:7890" in arguments
    assert (
        "--proxy-bypass-list=www.vikacg.com;bbs.yamibo.com" in arguments
    )
    assert not any("enable-automation" in value for value in arguments)
    await manager.close(info.id, save_state=False)


@pytest.mark.asyncio
async def test_deferred_chrome_attaches_only_when_checked_and_reloads_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, info, _process, _launch_calls = await _start_native_session(
        tmp_path, monkeypatch
    )
    context = FakeContext()
    context.page.url = "https://example.test/account"
    browser = FakeBrowser()
    browser.contexts = [context]
    playwright = FakeDeferredPlaywright(browser)
    manager._playwright = playwright  # type: ignore[assignment]

    assert playwright.chromium.connect_calls == []
    assert await manager.login_is_complete(info.id, selectors=("#signed-in",)) is True
    assert playwright.chromium.connect_calls == [
        (f"http://127.0.0.1:{manager._native_sessions[info.id].cdp_port}", 15_000)
    ]
    assert context.page.reload_calls == 1

    assert await manager.login_is_complete(info.id, selectors=("#signed-in",)) is True
    assert len(playwright.chromium.connect_calls) == 1
    assert context.page.reload_calls == 1
    await manager.close(info.id, save_state=False)


@pytest.mark.asyncio
async def test_deferred_chrome_save_returns_state_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, info, process, _launch_calls = await _start_native_session(
        tmp_path, monkeypatch
    )
    context = FakeContext()
    context.page.url = "https://example.test/account"
    context.page.session_storage_entries = [["auth", "test-only-token"]]
    browser = FakeBrowser()
    browser.contexts = [context]
    manager._playwright = FakeDeferredPlaywright(browser)  # type: ignore[assignment]

    async def accept_state(_state: dict) -> None:
        pass

    monkeypatch.setattr(manager, "_validate_storage_state", accept_state)
    profile_dir = manager._native_sessions[info.id].profile_dir
    state_json = await manager.close(info.id, save_state=True)
    state = json.loads(state_json or "{}")

    assert state["cookies"][0]["name"] == "session"
    assert state[SESSION_STORAGE_STATE_KEY][0]["entries"] == [
        ["auth", "test-only-token"]
    ]
    assert context.storage_state_indexed_db is True
    assert context.page.reload_calls == 1
    assert browser.closed is True
    assert process.terminated is True
    assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_deferred_chrome_capture_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, info, process, _launch_calls = await _start_native_session(
        tmp_path, monkeypatch
    )
    context = FakeContext()
    context.page.url = "https://example.test/account"
    browser = FakeBrowser()
    browser.contexts = [context]
    manager._playwright = FakeDeferredPlaywright(browser)  # type: ignore[assignment]

    async def capture_fails(_context) -> list[dict]:
        raise BrowserStorageStateError("capture failed")

    monkeypatch.setattr(manager, "_collect_falsey_indexeddb_keys", capture_fails)
    profile_dir = manager._native_sessions[info.id].profile_dir
    with pytest.raises(BrowserStorageStateError, match="capture failed"):
        await manager.close(info.id, save_state=True)

    assert process.terminated is True
    assert browser.closed is True
    assert not profile_dir.exists()
    assert info.id not in manager._native_sessions
    assert "native-account" not in manager._account_sessions


@pytest.mark.asyncio
async def test_deferred_chrome_start_failure_and_expiration_clean_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome.exe"
    await asyncio.to_thread(executable.touch)
    failed_process = FakeNativeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return failed_process

    async def debug_port_fails(_process, _port: int) -> None:
        raise BrowserStorageStateError("debug endpoint failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    manager = DeferredChromeBrowserSessionManager(
        executable_path=executable,
        profile_root=tmp_path / "profiles",
        timeout_seconds=10,
    )
    monkeypatch.setattr(manager, "_wait_for_debug_port", debug_port_fails)
    with pytest.raises(BrowserStorageStateError, match="debug endpoint failed"):
        await manager.start(
            account_id="failed-account",
            login_url="https://example.test/login",
        )
    assert failed_process.terminated is True
    assert list((tmp_path / "profiles").iterdir()) == []

    manager, info, expired_process, _launch_calls = await _start_native_session(
        tmp_path / "expiry", monkeypatch
    )
    profile_dir = manager._native_sessions[info.id].profile_dir
    manager._native_sessions[info.id].last_activity = datetime.now(UTC) - timedelta(
        seconds=11
    )
    assert await manager.cleanup_expired() == 1
    assert expired_process.terminated is True
    assert not profile_dir.exists()
    assert info.id not in manager._native_sessions


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
async def test_read_only_browser_polling_does_not_refresh_activity(caplog) -> None:
    manager = BrowserSessionManager(timeout_seconds=10)
    fake_browser = FakeBrowser()
    manager._browser = fake_browser
    info = await manager.start(
        account_id="account-polled",
        login_url="https://example.test/login",
    )
    unchanged_activity = datetime.now(UTC) - timedelta(seconds=5)
    manager._sessions[info.id].last_activity = unchanged_activity

    polled_info = await manager.get_info(info.id)
    assert await manager.screenshot(info.id) == b"fake-png"
    assert polled_info.last_activity == unchanged_activity
    assert manager._sessions[info.id].last_activity == unchanged_activity

    manager._sessions[info.id].last_activity = datetime.now(UTC) - timedelta(seconds=11)
    with caplog.at_level("INFO", logger="uvicorn.error.autosign.browser_sessions"):
        with pytest.raises(BrowserSessionNotFoundError, match="Expired browser session"):
            await manager.get_info(info.id)
    assert "Closed 1 expired interactive browser session(s)" in caplog.text
    assert fake_browser.contexts[0].closed is True


@pytest.mark.asyncio
async def test_explicit_browser_activity_refreshes_idle_deadline() -> None:
    manager = BrowserSessionManager(timeout_seconds=10)
    fake_browser = FakeBrowser()
    manager._browser = fake_browser
    info = await manager.start(
        account_id="account-active",
        login_url="https://example.test/login",
    )
    previous_activity = datetime.now(UTC) - timedelta(seconds=9)
    manager._sessions[info.id].last_activity = previous_activity

    await manager.mark_activity(info.id)

    assert manager._sessions[info.id].last_activity > previous_activity
    assert await manager.cleanup_expired() == 0
    await manager.close(info.id, save_state=False)


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
        self.started_login_urls: list[str] = []
        self.activity_calls = 0
        self.focus_calls = 0

    async def start(
        self,
        *,
        account_id: str,
        login_url: str,
        storage_state_json: str | None = None,
        **_kwargs,
    ) -> BrowserSessionInfo:
        self.started_login_urls.append(login_url)
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

    async def focus(self, _session_id: str) -> None:
        self.focus_calls += 1

    async def cleanup_expired(self) -> int:
        return 0

    async def screenshot(self, _session_id: str) -> bytes:
        return b"fake-png"

    async def mark_activity(self, _session_id: str) -> None:
        self.activity_calls += 1
        self.info = BrowserSessionInfo(
            id=self.info.id,
            account_id=self.info.account_id,
            url=self.info.url,
            title=self.info.title,
            created_at=self.info.created_at,
            last_activity=datetime.now(UTC),
        )

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
        assert started.json()["live_url"] == f"/browser-sessions/{session_id}/live"
        assert browser_manager.started_login_urls[-1].endswith("/demo-login")

        standalone = client.get(started.json()["live_url"])
        assert standalone.status_code == 200
        assert "AutoSign 独立登录浏览器" in standalone.text

        screenshot = client.get(f"/api/v1/browser-sessions/{session_id}/screenshot")
        assert screenshot.content == b"fake-png"
        assert screenshot.headers["cache-control"] == "no-store, max-age=0"

        activity = client.post(f"/api/v1/browser-sessions/{session_id}/activity")
        assert activity.status_code == 204
        assert browser_manager.activity_calls == 1

        focused = client.post(f"/api/v1/browser-sessions/{session_id}/focus")
        assert focused.status_code == 204
        assert browser_manager.focus_calls == 2  # live page load plus explicit focus

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

        vikacg_account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "vikacg", "label": "VikACG 登录入口测试"},
        ).json()
        vikacg_session = client.post(
            f"/api/v1/accounts/{vikacg_account['id']}/browser-session?clean=true"
        )
        assert vikacg_session.status_code == 200
        assert browser_manager.started_login_urls[-1] == (
            "https://www.vikacg.com/wallet/mission"
        )
        client.post(
            f"/api/v1/browser-sessions/{vikacg_session.json()['id']}/close",
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


def test_native_browser_session_uses_visible_system_window_contract(tmp_path: Path) -> None:
    settings = Settings(
        environment="testing",
        data_dir=tmp_path,
        master_key=SecretStr(SecretCipher.generate_key()),
        auth_disabled=True,
        browser_native_window=True,
    )
    browser_manager = FakeApiBrowserManager()
    app = create_app(settings, browser_manager_override=browser_manager)

    with TestClient(app) as client:
        account = client.post(
            "/api/v1/accounts",
            json={"plugin_id": "demo", "label": "系统 Chrome 测试"},
        ).json()
        started = client.post(f"/api/v1/accounts/{account['id']}/browser-session")

        assert started.status_code == 200
        assert started.json()["live_url"] is None
        focused = client.post(
            f"/api/v1/browser-sessions/{started.json()['id']}/focus"
        )
        assert focused.status_code == 204
        assert browser_manager.focus_calls == 1
