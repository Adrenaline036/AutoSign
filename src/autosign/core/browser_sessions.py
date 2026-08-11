from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from autosign.plugin_sdk import BrowserResponse

BROWSER_STATE_SECRET = "browser_storage_state"
SESSION_STORAGE_STATE_KEY = "_autosign_session_storage"
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
BROWSER_SESSION_LOGGER = logging.getLogger("uvicorn.error.autosign.browser_sessions")
PASSWORD_FORM_GUARD_SCRIPT = r"""
(() => {
  window.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "get") return;
    if (!form.querySelector('input[type="password"]')) return;
    event.preventDefault();
    console.error(
      "AutoSign blocked an unsafe native GET password submission because " +
      "the page login script may not be ready."
    );
  }, true);
})();
"""


class BrowserSessionNotFoundError(LookupError):
    pass


class BrowserSessionInputError(ValueError):
    pass


class BrowserStorageStateError(RuntimeError):
    pass


FALSEY_INDEXEDDB_KEYS_SCRIPT = r"""
async () => {
  const databases = [];
  for (const databaseInfo of await indexedDB.databases()) {
    if (!databaseInfo.name) continue;
    const database = await new Promise((resolve, reject) => {
      const request = indexedDB.open(databaseInfo.name);
      request.addEventListener("success", () => resolve(request.result));
      request.addEventListener("error", () => reject(request.error));
    });
    const stores = [];
    if (database.objectStoreNames.length) {
      const transaction = database.transaction(database.objectStoreNames, "readonly");
      for (const storeName of database.objectStoreNames) {
        const objectStore = transaction.objectStore(storeName);
        if (objectStore.keyPath !== null) continue;
        const keys = await new Promise((resolve, reject) => {
          const request = objectStore.getAllKeys();
          request.addEventListener("success", () => resolve(request.result));
          request.addEventListener("error", () => reject(request.error));
        });
        const records = [];
        keys.forEach((key, index) => {
          if (key === 0 || key === "") records.push({index, key});
        });
        if (records.length) stores.push({name: storeName, records});
      }
    }
    database.close();
    if (stores.length) databases.push({name: databaseInfo.name, stores});
  }
  return {origin: location.origin, databases};
}
"""
SESSION_STORAGE_CAPTURE_SCRIPT = r"""
() => ({origin: location.origin, entries: Object.entries(sessionStorage)})
"""


def normalize_storage_state(state: dict) -> tuple[dict, int]:
    """Repair Playwright 1.55 exports that omit falsey out-of-line IDB keys."""
    repaired = 0
    for origin in state.get("origins", []):
        for database in origin.get("indexedDB", []):
            for store in database.get("stores", []):
                if store.get("keyPath") is not None or store.get("keyPathArray") is not None:
                    continue
                if store.get("autoIncrement"):
                    continue
                missing = [
                    record
                    for record in store.get("records", [])
                    if record.get("key") is None and record.get("keyEncoded") is None
                ]
                # IndexedDB keys cannot be false/null/undefined. Of the primitive
                # keys Playwright treats as falsey, only numeric 0 and "" are valid.
                # getAllKeys() sorts numbers before strings, matching record order.
                for record, fallback_key in zip(missing, (0, ""), strict=False):
                    record["key"] = fallback_key
                    repaired += 1
                if len(missing) > 2:
                    invalid_ids = {id(record) for record in missing[2:]}
                    store["records"] = [
                        record
                        for record in store.get("records", [])
                        if id(record) not in invalid_ids
                    ]
    return state, repaired


def unpack_storage_state(state: dict) -> tuple[dict, list[dict]]:
    """Separate AutoSign extensions before passing state to Playwright."""
    session_storage = state.pop(SESSION_STORAGE_STATE_KEY, [])
    if not isinstance(session_storage, list):
        session_storage = []
    state, _ = normalize_storage_state(state)
    return state, session_storage


def session_storage_restore_script(session_storage: list[dict]) -> str | None:
    storage_by_origin: dict[str, dict[str, str]] = {}
    for item in session_storage:
        if not isinstance(item, dict):
            continue
        origin = item.get("origin")
        entries = item.get("entries")
        if not isinstance(origin, str) or not isinstance(entries, list):
            continue
        valid_entries: dict[str, str] = {}
        for entry in entries:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and all(isinstance(value, str) for value in entry)
            ):
                valid_entries[entry[0]] = entry[1]
        storage_by_origin[origin] = valid_entries
    if not storage_by_origin:
        return None
    encoded = json.dumps(storage_by_origin, ensure_ascii=False, separators=(",", ":"))
    return f"""
(() => {{
  const storageByOrigin = {encoded};
  const values = storageByOrigin[location.origin];
  if (!values) return;
  for (const [key, value] of Object.entries(values)) {{
    sessionStorage.setItem(key, value);
  }}
}})();
"""


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

    async def click(self, selector: str) -> bool:
        try:
            locator = self.page.locator(selector)
            for index in range(await locator.count()):
                candidate = locator.nth(index)
                if not await candidate.is_visible():
                    continue
                try:
                    await candidate.click(timeout=1_200)
                except Exception:
                    try:
                        clicked = await candidate.evaluate(
                            """element => {
                                const clickable = element.closest(
                                    'button, [role="button"], a, [onclick], ' +
                                    'input[type="button"], input[type="submit"]'
                                ) || element;
                                if (clickable.disabled) return false;
                                if (clickable.getAttribute('aria-disabled') === 'true') {
                                    return false;
                                }
                                clickable.scrollIntoView({block: 'center'});
                                clickable.click();
                                return true;
                            }"""
                        )
                    except Exception:
                        continue
                    if not clicked:
                        continue
                return True
        except Exception:
            return False
        return False

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

    async def post_json(
        self,
        url: str,
        data: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> BrowserResponse:
        response = await self.page.context.request.post(
            url,
            data=dict(data),
            headers=dict(headers or {}),
            timeout=30_000,
        )
        return BrowserResponse(
            status=response.status,
            url=response.url,
            text=await response.text(),
        )

    async def storage_value(self, origin: str, key: str) -> object | None:
        """Read a restored localStorage or IndexedDB value without navigation."""
        state = await self.page.context.storage_state(indexed_db=True)
        for origin_state in state.get("origins", []):
            if origin_state.get("origin") != origin:
                continue
            for item in origin_state.get("localStorage", []):
                if item.get("name") == key:
                    return item.get("value")
            for database in origin_state.get("indexedDB", []):
                for store in database.get("stores", []):
                    for record in store.get("records", []):
                        if record.get("key") == key:
                            return record.get("value")
        return None

    async def write_storage_value(self, key: str, value: object) -> bool:
        """Replace an existing localStorage or IndexedDB value on the current origin."""
        return bool(
            await self.page.evaluate(
                """async ({key, value}) => {
                    if (localStorage.getItem(key) !== null) {
                        localStorage.setItem(
                            key,
                            typeof value === "string" ? value : JSON.stringify(value),
                        );
                        return true;
                    }
                    for (const databaseInfo of await indexedDB.databases()) {
                        if (!databaseInfo.name) continue;
                        const database = await new Promise((resolve, reject) => {
                            const request = indexedDB.open(databaseInfo.name);
                            request.addEventListener("success", () => resolve(request.result));
                            request.addEventListener("error", () => reject(request.error));
                        });
                        try {
                            for (const storeName of database.objectStoreNames) {
                                const found = await new Promise((resolve, reject) => {
                                    const transaction = database.transaction(storeName, "readonly");
                                    const request = transaction.objectStore(storeName).get(key);
                                    request.addEventListener(
                                        "success",
                                        () => resolve(request.result !== undefined),
                                    );
                                    request.addEventListener("error", () => reject(request.error));
                                });
                                if (!found) continue;
                                await new Promise((resolve, reject) => {
                                    const transaction = database.transaction(
                                        storeName,
                                        "readwrite",
                                    );
                                    const store = transaction.objectStore(storeName);
                                    if (store.keyPath === null) store.put(value, key);
                                    else store.put(value);
                                    transaction.addEventListener("complete", () => resolve(true));
                                    transaction.addEventListener(
                                        "error",
                                        () => reject(transaction.error),
                                    );
                                    transaction.addEventListener(
                                        "abort",
                                        () => reject(transaction.error),
                                    );
                                });
                                return true;
                            }
                        } finally {
                            database.close();
                        }
                    }
                    return false;
                }""",
                {"key": key, "value": value},
            )
        )


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
    def __init__(
        self,
        timeout_seconds: int = 900,
        *,
        headless: bool = True,
        hide_window: bool = False,
        proxy_server: str | None = None,
        proxy_bypass: str | None = None,
    ) -> None:
        self._timeout = timedelta(seconds=timeout_seconds)
        self._headless = headless
        self._launch_args = ["--disable-dev-shm-usage"]
        if hide_window:
            self._launch_args.extend(
                [
                    "--window-position=-32000,-32000",
                    f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
                ]
            )
        self._proxy: dict[str, str] | None = None
        if proxy_server:
            self._proxy = {"server": proxy_server}
            if proxy_bypass:
                self._proxy["bypass"] = proxy_bypass
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, ActiveBrowserSession] = {}
        self._account_sessions: dict[str, str] = {}
        self._manager_lock = asyncio.Lock()
        self._logger = BROWSER_SESSION_LOGGER

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and not self._browser.is_connected():
            await self._reset_browser_locked()
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=self._launch_args,
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
            self._log_expired(await self._cleanup_expired_locked())
            existing_id = self._account_sessions.get(account_id)
            if existing_id is not None:
                await self._close_locked(existing_id, save_state=False)

            storage_state = None
            session_storage: list[dict] = []
            if storage_state_json:
                storage_state, session_storage = unpack_storage_state(
                    json.loads(storage_state_json)
                )
            for attempt in range(2):
                browser = await self._ensure_browser()
                context: BrowserContext | None = None
                try:
                    context = await browser.new_context(
                        storage_state=storage_state,
                        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        proxy=self._proxy,
                    )
                    restore_script = session_storage_restore_script(session_storage)
                    if restore_script is not None:
                        await context.add_init_script(script=restore_script)
                    await context.add_init_script(script=PASSWORD_FORM_GUARD_SCRIPT)
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
                    if (
                        attempt == 0
                        and storage_state is not None
                        and self._is_storage_state_error(exc)
                    ):
                        # Keep the encrypted state untouched, but allow the user to
                        # open a clean interactive browser and replace it by logging
                        # in again instead of trapping the account behind HTTP 502.
                        storage_state = None
                        session_storage = []
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
            self._bind_page_lifecycle(session, page)
            context.on("page", lambda opened_page: self._activate_page(session, opened_page))
            self._sessions[session.id] = session
            self._account_sessions[account_id] = session.id
            return await self._info(session)

    async def get_info(self, session_id: str) -> BrowserSessionInfo:
        session = await self._get(session_id)
        async with session.operation_lock:
            try:
                return await self._info(session)
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def focus(self, session_id: str) -> None:
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)
            try:
                await session.page.bring_to_front()
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise

    async def screenshot(self, session_id: str) -> bytes:
        session = await self._get(session_id)
        async with session.operation_lock:
            cdp_session = None
            try:
                # Playwright's high-level screenshot API waits for every web font
                # to finish loading.  A blocked third-party font can therefore
                # freeze the remote browser GUI for 30 seconds even though the page
                # itself is already interactive. Chromium's capture command takes
                # the current viewport immediately and does not wait for fonts.
                cdp_session = await session.context.new_cdp_session(session.page)
                result = await cdp_session.send(
                    "Page.captureScreenshot",
                    {
                        "format": "png",
                        "fromSurface": True,
                        "captureBeyondViewport": False,
                    },
                )
                return base64.b64decode(result["data"])
            except Exception as exc:
                await self._translate_target_closed(session, exc)
                raise
            finally:
                if cdp_session is not None:
                    with suppress(Exception):
                        await cdp_session.detach()

    async def mark_activity(self, session_id: str) -> None:
        """Refresh an interactive session only for an explicit user action."""
        session = await self._get(session_id)
        async with session.operation_lock:
            self._touch(session)

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
                # Insert text into the currently focused page control without
                # interpreting password symbols, CJK text, or modifier-looking
                # characters as physical keyboard shortcuts.
                await session.page.keyboard.insert_text(text)
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
        storage_state, session_storage = unpack_storage_state(
            json.loads(storage_state_json)
        )
        try:
            context = await browser.new_context(
                storage_state=storage_state,
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                proxy=self._proxy,
            )
        except Exception as exc:
            if self._is_storage_state_error(exc):
                raise BrowserStorageStateError(
                    "Saved browser login state cannot be restored. "
                    "Open interactive login and save the account again."
                ) from exc
            raise
        restore_script = session_storage_restore_script(session_storage)
        if restore_script is not None:
            await context.add_init_script(script=restore_script)
        await context.add_init_script(script=PASSWORD_FORM_GUARD_SCRIPT)
        page = await context.new_page()
        try:
            yield PlaywrightAutomationClient(page)
        finally:
            await context.close()

    async def capture_automation_state(
        self,
        browser: PlaywrightAutomationClient,
    ) -> str:
        """Capture state after a verified automated run for encrypted persistence.

        A site can rotate a session cookie or refresh token while a task is
        running. Without this capture, the next schedule restores only the
        original interactive-login state and can be logged out unexpectedly.
        """
        page = browser.page
        context = page.context
        falsey_keys = await self._collect_falsey_indexeddb_keys(context)
        session_storage = await self._collect_session_storage(context, page)
        state = await context.storage_state(indexed_db=True)
        self._apply_falsey_indexeddb_keys(state, falsey_keys)
        state, _ = normalize_storage_state(state)
        if session_storage:
            state[SESSION_STORAGE_STATE_KEY] = session_storage
        await self._validate_storage_state(state)
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    async def close_all(self) -> None:
        async with self._manager_lock:
            for session_id in list(self._sessions):
                await self._close_locked(session_id, save_state=False)
            await self._reset_browser_locked()

    async def cleanup_expired(self) -> int:
        """Close idle interactive sessions without waiting for another request."""
        async with self._manager_lock:
            expired_count = await self._cleanup_expired_locked()
        self._log_expired(expired_count)
        return expired_count

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
                expired = await self._expire_session_locked(session_id)
            if expired:
                self._log_expired(1)
                raise BrowserSessionNotFoundError(f"Expired browser session: {session_id}")
            session = self._sessions.get(session_id)
            if session is None:
                raise BrowserSessionNotFoundError(f"Unknown browser session: {session_id}")
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
                    falsey_keys = await self._collect_falsey_indexeddb_keys(session.context)
                    session_storage = await self._collect_session_storage(
                        session.context, session.page
                    )
                    # Some modern applications keep their authentication token in
                    # IndexedDB instead of cookies or localStorage.  Preserve it as
                    # part of the same encrypted browser state so a later automation
                    # context can restore the complete login session.
                    state = await session.context.storage_state(indexed_db=True)
                    self._apply_falsey_indexeddb_keys(state, falsey_keys)
                    state, _ = normalize_storage_state(state)
                    if session_storage:
                        state[SESSION_STORAGE_STATE_KEY] = session_storage
                    await self._validate_storage_state(state)
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

    async def _cleanup_expired_locked(self) -> int:
        expired_count = 0
        for session_id in list(self._sessions):
            if await self._expire_session_locked(session_id):
                expired_count += 1
        return expired_count

    async def _expire_session_locked(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        async with session.operation_lock:
            # Recheck after waiting for an in-flight browser operation. A request
            # may have refreshed last_activity while cleanup was waiting.
            if datetime.now(UTC) - session.last_activity <= self._timeout:
                return False
            self._discard_session(session)
            with suppress(Exception):
                await session.context.close()
        return True

    async def _collect_falsey_indexeddb_keys(
        self,
        context: BrowserContext,
    ) -> list[dict]:
        results: list[dict] = []
        seen_origins: set[str] = set()
        for page in context.pages:
            if page.is_closed():
                continue
            try:
                result = await asyncio.wait_for(
                    page.evaluate(FALSEY_INDEXEDDB_KEYS_SCRIPT),
                    timeout=5,
                )
            except Exception:
                continue
            origin = result.get("origin") if isinstance(result, dict) else None
            if not isinstance(origin, str) or origin in seen_origins:
                continue
            seen_origins.add(origin)
            results.append(result)
        return results

    async def _collect_session_storage(
        self,
        context: BrowserContext,
        primary_page: Page,
    ) -> list[dict]:
        results: list[dict] = []
        seen_origins: set[str] = set()
        other_pages = [page for page in context.pages if page is not primary_page]
        pages = [primary_page, *other_pages]
        for page in pages:
            if page.is_closed():
                continue
            try:
                result = await asyncio.wait_for(
                    page.evaluate(SESSION_STORAGE_CAPTURE_SCRIPT),
                    timeout=5,
                )
            except Exception:
                continue
            origin = result.get("origin") if isinstance(result, dict) else None
            entries = result.get("entries") if isinstance(result, dict) else None
            if (
                not isinstance(origin, str)
                or origin in seen_origins
                or not isinstance(entries, list)
                or not entries
            ):
                continue
            seen_origins.add(origin)
            results.append({"origin": origin, "entries": entries})
        return results

    @staticmethod
    def _apply_falsey_indexeddb_keys(state: dict, key_maps: list[dict]) -> None:
        origins = {origin.get("origin"): origin for origin in state.get("origins", [])}
        for key_map in key_maps:
            origin = origins.get(key_map.get("origin"))
            if origin is None:
                continue
            databases = {
                database.get("name"): database
                for database in origin.get("indexedDB", [])
            }
            for database_map in key_map.get("databases", []):
                database = databases.get(database_map.get("name"))
                if database is None:
                    continue
                stores = {
                    store.get("name"): store
                    for store in database.get("stores", [])
                }
                for store_map in database_map.get("stores", []):
                    store = stores.get(store_map.get("name"))
                    if store is None:
                        continue
                    records = store.get("records", [])
                    for record_map in store_map.get("records", []):
                        index = record_map.get("index")
                        if isinstance(index, int) and 0 <= index < len(records):
                            records[index]["key"] = record_map.get("key")

    async def _validate_storage_state(self, state: dict) -> None:
        browser = await self._ensure_browser()
        context: BrowserContext | None = None
        playwright_state, session_storage = unpack_storage_state(dict(state))
        try:
            context = await browser.new_context(
                storage_state=playwright_state,
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                proxy=self._proxy,
            )
            restore_script = session_storage_restore_script(session_storage)
            if restore_script is not None:
                await context.add_init_script(script=restore_script)
        except Exception as exc:
            raise BrowserStorageStateError(
                "The captured browser login state could not be restored safely. "
                "Reopen interactive login and save the account again."
            ) from exc
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()

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

    def _log_expired(self, expired_count: int) -> None:
        if expired_count:
            self._logger.info(
                "Closed %s expired interactive browser session(s)",
                expired_count,
            )

    def _activate_page(self, session: ActiveBrowserSession, page: Page) -> None:
        if page.is_closed() or session.id not in self._sessions:
            return
        session.page = page
        self._touch(session)
        self._bind_page_lifecycle(session, page)

    def _bind_page_lifecycle(self, session: ActiveBrowserSession, page: Page) -> None:
        page.on("close", lambda: self._restore_page_after_close(session, page))

    def _restore_page_after_close(self, session: ActiveBrowserSession, closed_page: Page) -> None:
        if session.id not in self._sessions or session.page is not closed_page:
            return
        remaining_pages = [page for page in session.context.pages if not page.is_closed()]
        if remaining_pages:
            session.page = remaining_pages[-1]
            self._touch(session)

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

    @staticmethod
    def _is_storage_state_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "error setting storage state" in message or "unable to restore indexeddb" in message


@dataclass(slots=True)
class DeferredChromeSession:
    id: str
    account_id: str
    login_url: str
    profile_dir: Path
    cdp_port: int
    process: asyncio.subprocess.Process
    created_at: datetime
    last_activity: datetime
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DeferredChromeBrowserSessionManager(BrowserSessionManager):
    """Launch ordinary Chrome first and attach Playwright only after user login."""

    def __init__(
        self,
        *,
        executable_path: Path,
        profile_root: Path,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._native_executable = executable_path.resolve()
        self._native_profile_root = profile_root.resolve()
        self._native_profile_root.mkdir(parents=True, exist_ok=True)
        self._native_sessions: dict[str, DeferredChromeSession] = {}

    async def start(
        self,
        *,
        account_id: str,
        login_url: str,
        storage_state_json: str | None = None,
    ) -> BrowserSessionInfo:
        del storage_state_json  # A dedicated clean Chrome profile is intentional here.
        async with self._manager_lock:
            await self._cleanup_expired_native_locked()
            existing_id = self._account_sessions.get(account_id)
            if existing_id is not None:
                await self._close_native_locked(existing_id, save_state=False)

            if not self._native_executable.is_file():
                raise BrowserStorageStateError(
                    f"Configured Chrome executable was not found: {self._native_executable}"
                )
            session_id = str(uuid4())
            profile_dir = (self._native_profile_root / session_id).resolve()
            if self._native_profile_root not in profile_dir.parents:
                raise BrowserStorageStateError("Unsafe native Chrome profile path.")
            profile_dir.mkdir(parents=True, exist_ok=False)
            cdp_port = self._available_loopback_port()
            launch_arguments = [
                str(self._native_executable),
                f"--user-data-dir={profile_dir}",
                f"--remote-debugging-port={cdp_port}",
                "--remote-debugging-address=127.0.0.1",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if os.name != "nt":
                # The NAS container runs Chromium as an unprivileged user on
                # Xvfb.  It must be a normal X11 process during login: no
                # Playwright launch(), headless mode or automation switches.
                launch_arguments.extend(
                    [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
                        "--lang=zh-CN",
                    ]
                )
            if self._proxy is not None:
                launch_arguments.append(f"--proxy-server={self._proxy['server']}")
                bypass = self._proxy.get("bypass")
                if bypass:
                    launch_arguments.append(
                        f"--proxy-bypass-list={bypass.replace(',', ';')}"
                    )
            launch_arguments.append(login_url)
            process = await asyncio.create_subprocess_exec(
                *launch_arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await self._wait_for_debug_port(process, cdp_port)
            except Exception:
                await self._terminate_process(process)
                await asyncio.to_thread(shutil.rmtree, profile_dir, True)
                raise

            now = datetime.now(UTC)
            session = DeferredChromeSession(
                id=session_id,
                account_id=account_id,
                login_url=login_url,
                profile_dir=profile_dir,
                cdp_port=cdp_port,
                process=process,
                created_at=now,
                last_activity=now,
            )
            self._native_sessions[session.id] = session
            self._account_sessions[account_id] = session.id
            return self._native_info(session)

    async def get_info(self, session_id: str) -> BrowserSessionInfo:
        session = await self._get_native(session_id)
        return self._native_info(session)

    async def focus(self, session_id: str) -> None:
        session = await self._get_native(session_id)
        self._touch_native(session)

    async def mark_activity(self, session_id: str) -> None:
        """Keep a noVNC-backed deferred session alive on real user input."""
        session = await self._get_native(session_id)
        async with session.operation_lock:
            self._touch_native(session)

    async def login_is_complete(
        self,
        session_id: str,
        *,
        selectors: tuple[str, ...],
        cookie_name_suffixes: tuple[str, ...] = (),
    ) -> bool:
        session = await self._get_native(session_id)
        async with session.operation_lock:
            self._touch_native(session)
            await self._attach_after_login(session)
            assert session.page is not None
            assert session.context is not None
            for selector in selectors:
                try:
                    if await session.page.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
            if cookie_name_suffixes:
                suffixes = tuple(suffix.lower() for suffix in cookie_name_suffixes)
                cookies = await session.context.cookies()
                if any(cookie["name"].lower().endswith(suffixes) for cookie in cookies):
                    return True
            return not selectors and not cookie_name_suffixes

    async def close(self, session_id: str, *, save_state: bool) -> str | None:
        async with self._manager_lock:
            return await self._close_native_locked(session_id, save_state=save_state)

    async def cleanup_expired(self) -> int:
        async with self._manager_lock:
            count = await self._cleanup_expired_native_locked()
        self._log_expired(count)
        return count

    async def close_all(self) -> None:
        async with self._manager_lock:
            for session_id in list(self._native_sessions):
                await self._close_native_locked(session_id, save_state=False)
        await super().close_all()

    async def _get_native(self, session_id: str) -> DeferredChromeSession:
        session = self._native_sessions.get(session_id)
        if session is None:
            raise BrowserSessionNotFoundError(f"Unknown browser session: {session_id}")
        if session.process.returncode is not None:
            self._discard_native(session)
            await asyncio.to_thread(shutil.rmtree, session.profile_dir, True)
            raise BrowserSessionNotFoundError(
                "Chrome login window was closed. Reopen interactive login."
            )
        if datetime.now(UTC) - session.last_activity > self._timeout:
            async with self._manager_lock:
                await self._close_native_locked(session_id, save_state=False)
            self._log_expired(1)
            raise BrowserSessionNotFoundError(f"Expired browser session: {session_id}")
        return session

    async def _attach_after_login(self, session: DeferredChromeSession) -> None:
        if session.browser is not None and session.browser.is_connected():
            return
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        session.browser = await self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{session.cdp_port}",
            timeout=15_000,
        )
        if not session.browser.contexts:
            raise BrowserStorageStateError("Chrome login profile has no browser context.")
        session.context = session.browser.contexts[0]
        pages = [page for page in session.context.pages if not page.is_closed()]
        if not pages:
            raise BrowserStorageStateError("Chrome login window has no open page.")
        target_host = self._hostname(session.login_url)
        session.page = next(
            (page for page in reversed(pages) if self._hostname(page.url) == target_host),
            pages[-1],
        )
        # A context attached over CDP does not know which origins were visited
        # before Playwright connected, so storage_state() would otherwise return
        # cookies but omit localStorage and IndexedDB. A single same-page reload
        # after the user has finished login registers the current origin while
        # preserving cookies, localStorage, sessionStorage and IndexedDB.
        await session.page.reload(wait_until="commit", timeout=30_000)
        with suppress(Exception):
            await session.page.wait_for_load_state("domcontentloaded", timeout=5_000)

    async def _close_native_locked(
        self,
        session_id: str,
        *,
        save_state: bool,
    ) -> str | None:
        session = self._native_sessions.pop(session_id, None)
        if session is None:
            raise BrowserSessionNotFoundError(f"Unknown browser session: {session_id}")
        self._account_sessions.pop(session.account_id, None)
        state_json: str | None = None
        async with session.operation_lock:
            try:
                if save_state:
                    await self._attach_after_login(session)
                    assert session.context is not None
                    assert session.page is not None
                    falsey_keys = await self._collect_falsey_indexeddb_keys(session.context)
                    session_storage = await self._collect_session_storage(
                        session.context, session.page
                    )
                    state = await session.context.storage_state(indexed_db=True)
                    self._apply_falsey_indexeddb_keys(state, falsey_keys)
                    state, _ = normalize_storage_state(state)
                    if session_storage:
                        state[SESSION_STORAGE_STATE_KEY] = session_storage
                    await self._validate_storage_state(state)
                    state_json = json.dumps(
                        state, ensure_ascii=False, separators=(",", ":")
                    )
            finally:
                await self._shutdown_native(session)
        return state_json

    async def _cleanup_expired_native_locked(self) -> int:
        count = 0
        for session_id, session in list(self._native_sessions.items()):
            if datetime.now(UTC) - session.last_activity <= self._timeout:
                continue
            await self._close_native_locked(session_id, save_state=False)
            count += 1
        return count

    async def _shutdown_native(self, session: DeferredChromeSession) -> None:
        if session.browser is not None and session.browser.is_connected():
            with suppress(Exception):
                await session.browser.close()
        await self._terminate_process(session.process)
        await asyncio.to_thread(shutil.rmtree, session.profile_dir, True)

    def _discard_native(self, session: DeferredChromeSession) -> None:
        self._native_sessions.pop(session.id, None)
        if self._account_sessions.get(session.account_id) == session.id:
            self._account_sessions.pop(session.account_id, None)

    @staticmethod
    def _native_info(session: DeferredChromeSession) -> BrowserSessionInfo:
        return BrowserSessionInfo(
            id=session.id,
            account_id=session.account_id,
            url=session.page.url if session.page is not None else session.login_url,
            title=(
                "普通 Chrome 登录窗口"
                if session.page is None
                else "Chrome 登录状态待保存"
            ),
            created_at=session.created_at,
            last_activity=session.last_activity,
        )

    @staticmethod
    def _touch_native(session: DeferredChromeSession) -> None:
        session.last_activity = datetime.now(UTC)

    @staticmethod
    def _hostname(url: str) -> str:
        from urllib.parse import urlparse

        return (urlparse(url).hostname or "").lower()

    @staticmethod
    def _available_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    async def _wait_for_debug_port(
        process: asyncio.subprocess.Process,
        port: int,
    ) -> None:
        for _ in range(80):
            if process.returncode is not None:
                raise BrowserStorageStateError("Chrome login window exited during startup.")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                del reader
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise BrowserStorageStateError("Chrome debugging endpoint did not start in time.")

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


class BrowserSessionCleanupCoordinator:
    def __init__(
        self,
        browser_sessions: BrowserSessionManager,
        *,
        poll_seconds: float = 60,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Browser cleanup poll interval must be positive.")
        self._browser_sessions = browser_sessions
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._logger = BROWSER_SESSION_LOGGER

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def poll_once(self) -> int:
        return await self._browser_sessions.cleanup_expired()

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Expired browser session cleanup failed")
            await asyncio.sleep(self._poll_seconds)
