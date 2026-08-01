from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from autosign.plugin_sdk import BrowserResponse

BROWSER_STATE_SECRET = "browser_storage_state"
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
ALLOWED_KEYS = {
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
}


class BrowserSessionNotFoundError(LookupError):
    pass


class BrowserSessionInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlaywrightAutomationClient:
    page: Page

    async def goto(self, url: str, *, referrer: str | None = None) -> int | None:
        options = {"wait_until": "commit", "timeout": 45_000}
        if referrer is not None:
            options["referer"] = referrer
        response = await self.page.goto(url, **options)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        return response.status if response is not None else None

    async def input_value(self, selector: str) -> str | None:
        try:
            return await self.page.locator(selector).first.input_value(timeout=3_000)
        except Exception:
            return None

    async def text_content(self, selector: str) -> str | None:
        try:
            text = await self.page.locator(selector).first.text_content(timeout=3_000)
        except Exception:
            return None
        return " ".join(text.split()) if text else None

    async def body_text(self) -> str:
        text = await self.page.locator("body").inner_text(timeout=5_000)
        return " ".join(text.split())

    async def html_content(self) -> str:
        return await self.page.content()

    async def post_form(
        self,
        url: str,
        data: Mapping[str, str],
    ) -> BrowserResponse:
        result = await self.page.evaluate(
            """async ({url, data}) => {
                const response = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    body: new URLSearchParams(data).toString(),
                });
                return {
                    status: response.status,
                    url: response.url,
                    text: await response.text(),
                };
            }""",
            {"url": url, "data": dict(data)},
        )
        return BrowserResponse(**result)


@dataclass(slots=True)
class ActiveBrowserSession:
    id: str
    account_id: str
    context: BrowserContext
    page: Page
    created_at: datetime
    last_activity: datetime
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True, slots=True)
class BrowserSessionInfo:
    id: str
    account_id: str
    url: str
    title: str
    created_at: datetime
    last_activity: datetime
    viewport_width: int = VIEWPORT_WIDTH
    viewport_height: int = VIEWPORT_HEIGHT


class BrowserSessionManager:
    def __init__(self, timeout_seconds: int = 900, *, headless: bool = True) -> None:
        self._timeout = timedelta(seconds=timeout_seconds)
        self._headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, ActiveBrowserSession] = {}
        self._account_sessions: dict[str, str] = {}
        self._manager_lock = asyncio.Lock()

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and not self._browser.is_connected():
            await self._reset_browser_locked()
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=["--disable-dev-shm-usage"],
            )
        return self._browser

    async def start(
        self,
        *,
        account_id: str,
        login_url: str,
        storage_state_json: str | None = None,
    ) -> BrowserSessionInfo:
        async with self._manager_lock:
            await self._cleanup_expired_locked()
            existing_id = self._account_sessions.get(account_id)
            if existing_id is not None:
                await self._close_locked(existing_id, save_state=False)

            storage_state = json.loads(storage_state_json) if storage_state_json else None
            for attempt in range(2):
                browser = await self._ensure_browser()
                context: BrowserContext | None = None
                try:
                    context = await browser.new_context(
                        storage_state=storage_state,
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                    )
                    page = await context.new_page()
                    await page.goto(login_url, wait_until="commit", timeout=45_000)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=3_000)
                    except Exception:
                        pass
                    break
                except Exception as exc:
                    if context is not None:
                        with suppress(Exception):
                            await context.close()
                    if attempt == 0 and self._is_target_closed_error(exc):
                        await self._reset_browser_locked()
                        continue
                    raise

            now = datetime.now(UTC)
            session = ActiveBrowserSession(
                id=str(uuid4()),
                account_id=account_id,
                context=context,
                page=page,
                created_at=now,
                last_activity=now,
            )
            self._sessions[session.id] = session
            self._account_sessions[account_id] = session.id
            return await self._info(session)

    async def get_info(self, session_id: str) -> BrowserSessionInfo:
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                return await self._info(session)
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def screenshot(self, session_id: str) -> bytes:
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                return await session.page.screenshot(type="png")
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def click(self, session_id: str, *, x: float, y: float) -> None:
        if not 0 <= x <= VIEWPORT_WIDTH or not 0 <= y <= VIEWPORT_HEIGHT:
            raise BrowserSessionInputError("Click coordinates are outside the browser viewport.")
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                await session.page.mouse.click(x, y)
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def type_text(self, session_id: str, *, text: str) -> None:
        if not text or len(text) > 4096:
            raise BrowserSessionInputError("Text input must contain 1-4096 characters.")
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                await session.page.keyboard.type(text)
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def press_key(self, session_id: str, *, key: str) -> None:
        if key not in ALLOWED_KEYS:
            raise BrowserSessionInputError(f"Unsupported browser key: {key}")
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                await session.page.keyboard.press(key)
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def login_is_complete(
        self,
        session_id: str,
        *,
        selectors: tuple[str, ...],
        cookie_name_suffixes: tuple[str, ...] = (),
    ) -> bool:
        if not selectors and not cookie_name_suffixes:
            return True
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                for selector in selectors:
                    try:
                        if await session.page.locator(selector).count() > 0:
                            return True
                    except Exception as exc:
                        if self._is_target_closed_error(exc):
                            raise
                if cookie_name_suffixes:
                    suffixes = tuple(suffix.lower() for suffix in cookie_name_suffixes)
                    cookies = await session.context.cookies()
                    if any(cookie["name"].lower().endswith(suffixes) for cookie in cookies):
                        return True
                return False
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def close(self, session_id: str, *, save_state: bool) -> str | None:
        async with self._manager_lock:
            return await self._close_locked(session_id, save_state=save_state)

    @asynccontextmanager
    async def automation(
        self,
        *,
        storage_state_json: str,
    ) -> AsyncIterator[PlaywrightAutomationClient]:
        browser = await self._ensure_browser()
        context = await browser.new_context(
            storage_state=json.loads(storage_state_json),
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await context.new_page()
        try:
            yield PlaywrightAutomationClient(page)
        finally:
            await context.close()

    async def close_all(self) -> None:
        async with self._manager_lock:
            for session_id in list(self._sessions):
                await self._close_locked(session_id, save_state=False)
            await self._reset_browser_locked()

    async def _get(self, session_id: str) -> ActiveBrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise BrowserSessionNotFoundError(f"Unknown browser session: {session_id}")
        if session.page.is_closed() or (
            self._browser is not None and not self._browser.is_connected()
        ):
            self._discard_session(session)
            raise BrowserSessionNotFoundError(
                "Browser session closed unexpectedly. Reopen interactive login."
            )
        if datetime.now(UTC) - session.last_activity > self._timeout:
            async with self._manager_lock:
                await self._close_locked(session_id, save_state=False)
            raise BrowserSessionNotFoundError(f"Expired browser session: {session_id}")
        return session

    async def _close_locked(self, session_id: str, *, save_state: bool) -> str | None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise BrowserSessionNotFoundError(f"Unknown browser session: {session_id}")
        self._account_sessions.pop(session.account_id, None)
        state_json: str | None = None
        async with session.operation_lock:
            try:
                if save_state:
                    state = await session.context.storage_state()
                    state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
            except Exception as exc:
                if self._is_target_closed_error(exc):
                    raise BrowserSessionNotFoundError(
                        "Browser session closed before its login state could be saved. "
                        "Reopen interactive login."
                    ) from exc
                raise
            finally:
                with suppress(Exception):
                    await session.context.close()
        return state_json

    async def _cleanup_expired_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_activity > self._timeout
        ]
        for session_id in expired:
            await self._close_locked(session_id, save_state=False)

    async def _info(self, session: ActiveBrowserSession) -> BrowserSessionInfo:
        return BrowserSessionInfo(
            id=session.id,
            account_id=session.account_id,
            url=session.page.url,
            title=await session.page.title(),
            created_at=session.created_at,
            last_activity=session.last_activity,
        )

    @staticmethod
    def _touch(session: ActiveBrowserSession) -> None:
        session.last_activity = datetime.now(UTC)

    def _discard_session(self, session: ActiveBrowserSession) -> None:
        self._sessions.pop(session.id, None)
        if self._account_sessions.get(session.account_id) == session.id:
            self._account_sessions.pop(session.account_id, None)

    async def _translate_target_closed(
        self,
        session: ActiveBrowserSession,
        exc: Exception,
    ) -> None:
        if not self._is_target_closed_error(exc):
            return
        self._discard_session(session)
        with suppress(Exception):
            await session.context.close()
        raise BrowserSessionNotFoundError(
            "Browser session closed unexpectedly. Reopen interactive login."
        ) from exc

    async def _reset_browser_locked(self) -> None:
        for session in list(self._sessions.values()):
            self._discard_session(session)
            with suppress(Exception):
                await session.context.close()
        if self._browser is not None:
            with suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    @staticmethod
    def _is_target_closed_error(exc: Exception) -> bool:
        return type(exc).__name__ == "TargetClosedError" or (
            "target page, context or browser has been closed" in str(exc).lower()
        )
