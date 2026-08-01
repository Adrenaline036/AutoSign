from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    BrowserSessionInfo,
    BrowserSessionManager,
    BrowserSessionNotFoundError,
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
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str) -> None:
        self.typed.append(text)

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeLocator:
    @property
    def first(self):
        return self

    async def is_visible(self, **_kwargs) -> bool:
        return True

    async def count(self) -> int:
        return 1


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.closed = False
        self.screenshot_error: Exception | None = None

    async def goto(self, url: str, **_kwargs) -> None:
        self.url = url

    async def title(self) -> str:
        return "Fake Login"

    async def screenshot(self, **_kwargs) -> bytes:
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return b"fake-png"

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator()

    def is_closed(self) -> bool:
        return self.closed


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def storage_state(self) -> dict:
        return {"cookies": [{"name": "session", "value": "encrypted-later"}], "origins": []}

    async def cookies(self) -> list[dict[str, str]]:
        return [{"name": "forum_auth", "value": "not-printed"}]

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.received_storage_state = None
        self.closed = False

    async def new_context(self, *, storage_state=None, **_kwargs) -> FakeContext:
        self.received_storage_state = storage_state
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed


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

    await manager.click(info.id, x=100, y=200)
    await manager.type_text(info.id, text="username")
    await manager.press_key(info.id, key="Enter")
    assert await manager.login_is_complete(info.id, selectors=("#logged-in",)) is True
    assert await manager.login_is_complete(
        info.id,
        selectors=(),
        cookie_name_suffixes=("_auth",),
    ) is True

    page = fake_browser.contexts[0].page
    assert page.mouse.clicks == [(100, 200)]
    assert page.keyboard.typed == ["username"]
    assert page.keyboard.pressed == ["Enter"]

    state_json = await manager.close(info.id, save_state=True)
    assert json.loads(state_json)["cookies"][0]["name"] == "session"
    assert fake_browser.contexts[0].closed is True
    with pytest.raises(BrowserSessionNotFoundError):
        await manager.get_info(info.id)


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

    async def start(self, *, account_id: str, **_kwargs) -> BrowserSessionInfo:
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
