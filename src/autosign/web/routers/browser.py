from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, Response
from starlette.websockets import WebSocketDisconnect

from autosign.core.auth import SESSION_COOKIE_NAME, AdminAuthService
from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    BrowserSessionInfo,
    BrowserSessionInputError,
    BrowserSessionManager,
    BrowserSessionNotFoundError,
    BrowserStorageStateError,
)
from autosign.core.config import Settings
from autosign.core.plugin_registry import PluginRegistry
from autosign.core.services import AccountService, VaultService
from autosign.core.services.accounts import AccountNotFoundError
from autosign.plugin_sdk import PluginCapability
from autosign.web.errors import account_error
from autosign.web.schemas import (
    BrowserClick,
    BrowserKeyInput,
    BrowserSessionClose,
    BrowserSessionCloseResult,
    BrowserSessionRead,
    BrowserTextInput,
)

STATIC_DIR = Path(__file__).parents[1] / "static"


def create_browser_router(
    *,
    settings: Settings,
    auth: AdminAuthService,
    accounts: AccountService,
    registry: PluginRegistry,
    vault: VaultService,
    browser_sessions: BrowserSessionManager,
    screenshot_interaction_enabled: bool,
) -> APIRouter:
    router = APIRouter()

    def serialize(info: BrowserSessionInfo) -> BrowserSessionRead:
        return BrowserSessionRead(
            id=info.id,
            account_id=info.account_id,
            url=info.url,
            title=info.title,
            created_at=info.created_at,
            last_activity=info.last_activity,
            viewport_width=info.viewport_width,
            viewport_height=info.viewport_height,
            live_url=(
                None
                if settings.browser_native_window
                else f"/browser-sessions/{info.id}/live"
            ),
        )

    def browser_error(exc: Exception) -> HTTPException:
        if isinstance(exc, BrowserSessionNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, BrowserSessionInputError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, BrowserStorageStateError):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(
            status_code=502,
            detail=f"Browser operation failed safely: {type(exc).__name__}",
        )

    def require_screenshot_interaction() -> None:
        if not screenshot_interaction_enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Screenshot browser controls are unavailable for this interactive "
                    "browser transport. Use the live noVNC or native Chrome window."
                ),
            )

    @router.post(
        "/api/v1/accounts/{account_id}/browser-session",
        response_model=BrowserSessionRead,
    )
    async def start_browser_session(
        account_id: str,
        request: Request,
        clean: bool | None = Query(
            None,
            deprecated=True,
            description=(
                "Deprecated and ignored. Interactive login always starts clean; "
                "saved state is restored only for automated execution."
            ),
        ),
    ) -> BrowserSessionRead:
        try:
            account = accounts.get(account_id)
            plugin = registry.get(account.plugin_id)
        except (AccountNotFoundError, LookupError) as exc:
            raise account_error(exc) from exc
        manifest = plugin.manifest
        if (
            PluginCapability.INTERACTIVE_LOGIN not in manifest.capabilities
            or manifest.login_url is None
        ):
            raise HTTPException(
                status_code=409,
                detail="This plugin does not support interactive browser login.",
            )

        if manifest.login_url.startswith("/"):
            login_url = urljoin(
                f"http://127.0.0.1:{settings.port}",
                manifest.login_url,
            )
        else:
            login_url = urljoin(str(request.base_url), manifest.login_url)
        try:
            info = await browser_sessions.start(
                account_id=account.id,
                login_url=login_url,
                storage_state_json=None,
            )
            return serialize(info)
        except Exception as exc:
            raise browser_error(exc) from exc

    @router.get(
        "/api/v1/browser-sessions/{session_id}",
        response_model=BrowserSessionRead,
    )
    async def get_browser_session(session_id: str) -> BrowserSessionRead:
        try:
            return serialize(await browser_sessions.get_info(session_id))
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post("/api/v1/browser-sessions/{session_id}/focus", status_code=204)
    async def focus_browser_session(session_id: str) -> None:
        try:
            await browser_sessions.focus(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.get("/browser-sessions/{session_id}/live", include_in_schema=False)
    async def live_browser(session_id: str) -> FileResponse:
        if not settings.browser_live_enabled:
            require_screenshot_interaction()
        try:
            await browser_sessions.focus(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc
        return FileResponse(
            STATIC_DIR
            / ("live_browser.html" if settings.browser_live_enabled else "remote_browser.html"),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.websocket("/api/v1/browser-sessions/{session_id}/vnc")
    async def browser_vnc(session_id: str, websocket: WebSocket) -> None:
        payload = (
            {"csrf": "testing-csrf"}
            if settings.auth_disabled
            else auth.verify_session(websocket.cookies.get(SESSION_COOKIE_NAME))
        )
        if payload is None:
            await websocket.close(code=4401)
            return

        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host")
        if origin and host and urlparse(origin).netloc != host:
            await websocket.close(code=4403)
            return
        if not settings.browser_live_enabled:
            await websocket.close(code=4404, reason="Live browser transport is disabled.")
            return

        try:
            await browser_sessions.focus(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError):
            await websocket.close(code=4404)
            return

        try:
            reader, writer = await asyncio.open_connection(
                settings.browser_vnc_host,
                settings.browser_vnc_port,
            )
        except OSError:
            await websocket.close(code=1011, reason="The live browser service is unavailable.")
            return

        offered_protocols = {
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        }
        await websocket.accept(
            subprotocol="binary" if "binary" in offered_protocols else None
        )

        async def websocket_to_vnc() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None and message.get("text") is not None:
                    data = message["text"].encode()
                if data:
                    writer.write(data)
                    await writer.drain()

        async def vnc_to_websocket() -> None:
            while data := await reader.read(65536):
                await websocket.send_bytes(data)

        tasks = {
            asyncio.create_task(websocket_to_vnc()),
            asyncio.create_task(vnc_to_websocket()),
        }
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, WebSocketDisconnect, OSError):
                    await task
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    @router.get("/api/v1/browser-sessions/{session_id}/screenshot", deprecated=True)
    async def browser_screenshot(session_id: str) -> Response:
        require_screenshot_interaction()
        try:
            image = await browser_sessions.screenshot(session_id)
            return Response(
                content=image,
                media_type="image/png",
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post("/api/v1/browser-sessions/{session_id}/activity", status_code=204)
    async def browser_activity(session_id: str) -> None:
        try:
            await browser_sessions.mark_activity(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post(
        "/api/v1/browser-sessions/{session_id}/click",
        status_code=204,
        deprecated=True,
    )
    async def browser_click(session_id: str, request: BrowserClick) -> None:
        require_screenshot_interaction()
        try:
            await browser_sessions.click(session_id, x=request.x, y=request.y)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post(
        "/api/v1/browser-sessions/{session_id}/type",
        status_code=204,
        deprecated=True,
    )
    async def browser_type_text(session_id: str, request: BrowserTextInput) -> None:
        require_screenshot_interaction()
        try:
            await browser_sessions.type_text(session_id, text=request.text)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post(
        "/api/v1/browser-sessions/{session_id}/press",
        status_code=204,
        deprecated=True,
    )
    async def browser_press_key(session_id: str, request: BrowserKeyInput) -> None:
        require_screenshot_interaction()
        try:
            await browser_sessions.press_key(session_id, key=request.key)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @router.post(
        "/api/v1/browser-sessions/{session_id}/close",
        response_model=BrowserSessionCloseResult,
    )
    async def close_browser_session(
        session_id: str,
        request: BrowserSessionClose,
    ) -> BrowserSessionCloseResult:
        try:
            info = await browser_sessions.get_info(session_id)
            verified = False
            if request.save_state:
                account = accounts.get(info.account_id)
                plugin = registry.get(account.plugin_id)
                verified = await browser_sessions.login_is_complete(
                    session_id,
                    selectors=plugin.manifest.login_success_selectors,
                    cookie_name_suffixes=plugin.manifest.login_cookie_name_suffixes,
                )
                if not verified and not request.force_save:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "尚未自动检测到登录状态。你可以继续登录，"
                            "或确认后仍然加密保存当前浏览器状态。"
                        ),
                    )
            state_json = await browser_sessions.close(
                session_id,
                save_state=request.save_state,
            )
            if state_json is not None:
                vault.set(info.account_id, BROWSER_STATE_SECRET, state_json)
            return BrowserSessionCloseResult(
                saved=state_json is not None,
                verified=verified,
                secret_names=vault.list_names(info.account_id),
            )
        except HTTPException:
            raise
        except (
            AccountNotFoundError,
            BrowserSessionNotFoundError,
            BrowserSessionInputError,
            BrowserStorageStateError,
            LookupError,
        ) as exc:
            raise browser_error(exc) from exc

    return router
