from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from autosign import __version__
from autosign.core.account_operations import AccountOperationGate
from autosign.core.auth import (
    SESSION_COOKIE_NAME,
    AdminAuthService,
    AuthConfigurationError,
)
from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    BrowserSessionCleanupCoordinator,
    BrowserSessionInfo,
    BrowserSessionInputError,
    BrowserSessionManager,
    BrowserSessionNotFoundError,
    BrowserStorageStateError,
    DeferredChromeBrowserSessionManager,
)
from autosign.core.config import Settings, get_settings
from autosign.core.db import Database
from autosign.core.login_limiter import LoginAttemptLimiter
from autosign.core.plugin_registry import PluginRegistry
from autosign.core.runner import PluginRunner
from autosign.core.security import SecretCipher
from autosign.core.services import (
    AccountService,
    ExecutionService,
    NotificationChannelService,
    ScheduleCoordinator,
    ScheduleService,
    VaultService,
)
from autosign.core.services.accounts import AccountNotFoundError
from autosign.core.services.backups import BackupCoordinator, BackupService
from autosign.plugin_sdk import PluginCapability, PluginManifest, SignResult
from autosign.web.features.vikacg_recovery import create_vikacg_recovery_router
from autosign.web.routers.accounts import create_accounts_router
from autosign.web.routers.backups import create_backups_router
from autosign.web.routers.executions import create_executions_router
from autosign.web.routers.notifications import create_notifications_router
from autosign.web.schemas import (
    AdminPasswordRequest,
    AuthStatus,
    BrowserCapacityRead,
    BrowserClick,
    BrowserKeyInput,
    BrowserSessionClose,
    BrowserSessionCloseResult,
    BrowserSessionRead,
    BrowserTextInput,
    CapacityPoolRead,
    CoordinatorStatusRead,
    SchedulerStatusRead,
    SystemStatusRead,
)

STATIC_DIR = Path(__file__).with_name("static")


def create_app(
    settings_override: Settings | None = None,
    browser_manager_override: BrowserSessionManager | None = None,
) -> FastAPI:
    settings = settings_override or get_settings()
    app_started_at = datetime.now(UTC)
    master_key = settings.require_master_key()
    registry = PluginRegistry()
    runner = PluginRunner(registry)
    database = Database(
        settings.database_url,
        sqlite_busy_timeout_ms=settings.database_busy_timeout_ms,
    )
    accounts = AccountService(database)
    cipher = SecretCipher(master_key)
    vault = VaultService(database, cipher)
    auth = AdminAuthService(
        database,
        master_key,
        session_hours=settings.auth_session_hours,
    )
    browser_options = {
        "timeout_seconds": settings.browser_session_timeout_seconds,
        "headless": settings.browser_headless,
        "hide_window": settings.browser_hide_window,
        "proxy_server": (
            settings.browser_proxy_server.get_secret_value()
            if settings.browser_proxy_server is not None
            else None
        ),
        "proxy_bypass": settings.browser_proxy_bypass,
        "automation_capacity": settings.browser_automation_capacity,
        "interactive_capacity": settings.browser_interactive_capacity,
    }
    deferred_transport_error = (
        "Deferred Chrome interactive login requires either "
        "AUTOSIGN_BROWSER_LIVE_ENABLED=true or "
        "AUTOSIGN_BROWSER_NATIVE_WINDOW=true."
    )
    if (
        browser_manager_override is None
        and settings.browser_native_window
        and settings.browser_native_executable is None
    ):
        raise RuntimeError(
            "AUTOSIGN_BROWSER_NATIVE_WINDOW=true requires "
            "AUTOSIGN_BROWSER_NATIVE_EXECUTABLE."
        )
    if (
        browser_manager_override is None
        and settings.browser_native_executable is not None
        and not settings.browser_live_enabled
        and not settings.browser_native_window
    ):
        raise RuntimeError(deferred_transport_error)
    if browser_manager_override is not None:
        browser_sessions = browser_manager_override
    elif settings.browser_native_executable is not None:
        browser_sessions = DeferredChromeBrowserSessionManager(
            executable_path=settings.browser_native_executable,
            profile_root=settings.data_dir / "browser-login-profiles",
            **browser_options,
        )
    else:
        browser_sessions = BrowserSessionManager(**browser_options)
    supports_screenshot_interaction = getattr(
        browser_sessions,
        "supports_screenshot_interaction",
        True,
    )
    if (
        browser_manager_override is not None
        and not supports_screenshot_interaction
        and not settings.browser_live_enabled
        and not settings.browser_native_window
    ):
        raise RuntimeError(deferred_transport_error)
    screenshot_interaction_enabled = (
        supports_screenshot_interaction and not settings.browser_live_enabled
    )
    browser_cleanup = BrowserSessionCleanupCoordinator(
        browser_sessions,
        poll_seconds=settings.browser_session_cleanup_poll_seconds,
    )
    executions = ExecutionService(database, runner)
    account_operations = AccountOperationGate()
    schedules = ScheduleService(database)
    notifications = NotificationChannelService(database, cipher)
    backup_password = (
        settings.backup_password.get_secret_value()
        if settings.backup_password is not None
        else None
    )
    backups = BackupService(
        database,
        database_path=settings.data_dir / "autosign.db",
        backup_dir=settings.data_dir / "backups",
        master_key=master_key,
        cipher=cipher,
        password=backup_password,
        autosign_version=__version__,
        enabled=settings.backup_enabled,
        daily_time=settings.backup_daily_time,
        timezone=settings.backup_timezone,
        retention_count=settings.backup_retention_count,
    )
    backup_coordinator = BackupCoordinator(
        backups,
        poll_seconds=settings.backup_poll_seconds,
    )

    async def run_account(
        account_id: str,
        trigger: str = "manual",
        attempt: int = 1,
    ) -> SignResult:
        async with account_operations.use(account_id):
            account = accounts.get(account_id)
            if not account.enabled:
                raise ValueError("Disabled accounts cannot be executed.")
            plugin = registry.get(account.plugin_id)
            storage_state_json: str | None = None
            if PluginCapability.BROWSER_SIGN in plugin.manifest.capabilities:
                try:
                    storage_state_json = vault.get(account.id, BROWSER_STATE_SECRET)
                except LookupError:
                    pass
            execute_options = {
                "account_id": account.id,
                "account_label": account.label,
                "settings": account.settings_json,
                "secrets": vault.for_account(account.id),
                "trigger": trigger,
                "attempt": attempt,
            }
            if storage_state_json is None:
                return await executions.execute(account.plugin_id, **execute_options)
            async with browser_sessions.automation(
                storage_state_json=storage_state_json,
            ) as browser:
                result = await executions.execute(
                    account.plugin_id,
                    browser=browser,
                    **execute_options,
                )
                if result.verified:
                    refreshed_state = await browser_sessions.capture_automation_state(
                        browser
                    )
                    vault.set(account.id, BROWSER_STATE_SECRET, refreshed_state)
                return result

    async def notify_final_result(account_id: str, result: SignResult) -> None:
        account = accounts.get(account_id)
        deliveries = await notifications.send_result(
            account_id,
            account_label=account.label,
            plugin_id=account.plugin_id,
            result=result,
        )
        record_id = result.details.get("execution_record_id")
        if isinstance(record_id, str):
            executions.annotate(
                record_id,
                {
                    "notification_deliveries": [
                        {
                            "channel_id": delivery.channel_id,
                            "channel_name": delivery.channel_name,
                            "channel_type": delivery.channel_type,
                            "success": delivery.success,
                            "message": delivery.message,
                        }
                        for delivery in deliveries
                    ]
                },
            )

    scheduler = ScheduleCoordinator(
        schedules,
        run_account,
        notify_final_result,
        poll_seconds=settings.scheduler_poll_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.prepare_directories()
        database.migrate()
        vault.initialize_key_check()
        backups.initialize()
        notifications.migrate_legacy(vault)
        registry.discover()
        scheduler.start()
        backup_coordinator.start()
        browser_cleanup.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await backup_coordinator.stop()
            await browser_cleanup.stop()
            await browser_sessions.close_all()
            database.dispose()

    app = FastAPI(
        title="AutoSign",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.runner = runner
    app.state.database = database
    app.state.accounts = accounts
    app.state.vault = vault
    app.state.auth = auth
    app.state.browser_sessions = browser_sessions
    app.state.browser_supports_screenshot_interaction = supports_screenshot_interaction
    app.state.browser_cleanup = browser_cleanup
    app.state.executions = executions
    app.state.account_operations = account_operations
    app.state.schedules = schedules
    app.state.scheduler = scheduler
    app.state.notifications = notifications
    app.state.backups = backups
    app.state.backup_coordinator = backup_coordinator
    login_limiter = LoginAttemptLimiter()

    if settings.browser_live_enabled:
        if not settings.browser_novnc_root.is_dir():
            raise RuntimeError(
                f"noVNC assets were not found at {settings.browser_novnc_root}. "
                "Disable AUTOSIGN_BROWSER_LIVE_ENABLED or install noVNC."
            )
        app.mount(
            "/novnc",
            StaticFiles(directory=settings.browser_novnc_root),
            name="novnc",
        )

    app.include_router(
        create_vikacg_recovery_router(
            accounts=accounts,
            registry=registry,
            vault=vault,
            browser_sessions=browser_sessions,
        )
    )
    app.include_router(create_backups_router(backups=backups))
    app.include_router(create_executions_router(executions=executions))
    app.include_router(create_notifications_router(notifications=notifications))
    app.include_router(
        create_accounts_router(
            accounts=accounts,
            vault=vault,
            registry=registry,
            account_operations=account_operations,
            notifications=notifications,
            schedules=schedules,
            run_account=run_account,
            notify_final_result=notify_final_result,
        )
    )

    def authenticated_payload(request: Request) -> dict[str, object] | None:
        if settings.auth_disabled:
            return {"csrf": "testing-csrf"}
        return auth.verify_session(request.cookies.get(SESSION_COOKIE_NAME))

    @app.middleware("http")
    async def protect_management_interface(request: Request, call_next):
        path = request.url.path
        payload = authenticated_payload(request)
        request.state.auth_payload = payload
        public_paths = {
            "/",
            "/healthz",
            "/demo-login",
            "/api/v1/auth/status",
            "/api/v1/auth/setup",
            "/api/v1/auth/login",
        }
        if path not in public_paths and payload is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Administrator login is required."},
            )
        if (
            not settings.auth_disabled
            and
            path not in public_paths
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("X-AutoSign-CSRF") != payload.get("csrf")
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid or missing CSRF token."},
            )
        return await call_next(request)

    def account_error(exc: Exception) -> HTTPException:
        if isinstance(exc, AccountNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    @app.get("/", include_in_schema=False)
    async def dashboard(request: Request) -> FileResponse:
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Vary": "Cookie",
        }
        if request.state.auth_payload is None:
            return FileResponse(STATIC_DIR / "auth.html", headers=headers)
        return FileResponse(STATIC_DIR / "index.html", headers=headers)

    @app.get("/demo-login", include_in_schema=False)
    async def demo_login() -> FileResponse:
        return FileResponse(STATIC_DIR / "demo_login.html")

    def set_auth_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=settings.auth_session_hours * 3600,
            httponly=True,
            secure=settings.auth_secure_cookie,
            samesite="strict",
            path="/",
        )

    @app.get("/api/v1/auth/status", response_model=AuthStatus)
    async def auth_status(request: Request, response: Response) -> AuthStatus:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Vary"] = "Cookie"
        payload = request.state.auth_payload
        return AuthStatus(
            configured=settings.auth_disabled or auth.is_configured(),
            authenticated=payload is not None,
            csrf_token=str(payload["csrf"]) if payload is not None else None,
        )

    @app.post("/api/v1/auth/setup", response_model=AuthStatus)
    async def setup_admin(
        request_data: AdminPasswordRequest,
        response: Response,
    ) -> AuthStatus:
        if settings.auth_disabled:
            raise HTTPException(status_code=409, detail="Authentication is disabled.")
        try:
            auth.setup(request_data.password.get_secret_value())
        except AuthConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = auth.issue_session()
        set_auth_cookie(response, session.token)
        return AuthStatus(
            configured=True,
            authenticated=True,
            csrf_token=session.csrf_token,
        )

    @app.post("/api/v1/auth/login", response_model=AuthStatus)
    async def login_admin(
        request: Request,
        request_data: AdminPasswordRequest,
        response: Response,
    ) -> AuthStatus:
        if not auth.is_configured():
            raise HTTPException(status_code=409, detail="Administrator password is not configured.")
        # Deliberately use the direct peer address. X-Forwarded-For is untrusted
        # until AutoSign has an explicit trusted-proxy configuration contract.
        client_key = request.client.host if request.client else "unknown"
        if login_limiter.is_limited(client_key):
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again in one minute.",
            )
        if not auth.verify_password(request_data.password.get_secret_value()):
            login_limiter.record_failure(client_key)
            raise HTTPException(status_code=401, detail="Administrator password is incorrect.")
        login_limiter.clear(client_key)
        session = auth.issue_session()
        set_auth_cookie(response, session.token)
        return AuthStatus(
            configured=True,
            authenticated=True,
            csrf_token=session.csrf_token,
        )

    @app.post("/api/v1/auth/logout", response_model=AuthStatus)
    async def logout_admin(response: Response) -> AuthStatus:
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return AuthStatus(configured=True, authenticated=False)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/system/status", response_model=SystemStatusRead)
    async def system_status() -> SystemStatusRead:
        capacity = await browser_sessions.capacity_snapshot()
        return SystemStatusRead(
            version=__version__,
            uptime_seconds=max(
                0,
                int((datetime.now(UTC) - app_started_at).total_seconds()),
            ),
            browser_capacity=BrowserCapacityRead(
                automation=CapacityPoolRead(
                    limit=capacity.automation_limit,
                    active=capacity.automation_active,
                    waiting=capacity.automation_waiting,
                ),
                interactive=CapacityPoolRead(
                    limit=capacity.interactive_limit,
                    active=capacity.interactive_active,
                    waiting=capacity.interactive_waiting,
                ),
                closing=capacity.closing,
            ),
            scheduler=SchedulerStatusRead(
                running=scheduler.running,
                active_jobs=scheduler.active_job_count,
            ),
            coordinators=CoordinatorStatusRead(
                browser_cleanup_running=browser_cleanup.running,
                backup_running=backup_coordinator.running,
            ),
        )

    @app.get("/api/v1/plugins", response_model=list[PluginManifest])
    async def list_plugins() -> list[PluginManifest]:
        return [plugin.manifest for plugin in registry.all()]

    def serialize_browser_session(info: BrowserSessionInfo) -> BrowserSessionRead:
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

    @app.post(
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
            return serialize_browser_session(info)
        except Exception as exc:
            raise browser_error(exc) from exc

    @app.get(
        "/api/v1/browser-sessions/{session_id}",
        response_model=BrowserSessionRead,
    )
    async def get_browser_session(session_id: str) -> BrowserSessionRead:
        try:
            return serialize_browser_session(await browser_sessions.get_info(session_id))
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @app.post("/api/v1/browser-sessions/{session_id}/focus", status_code=204)
    async def focus_browser_session(session_id: str) -> None:
        try:
            await browser_sessions.focus(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @app.get("/browser-sessions/{session_id}/live", include_in_schema=False)
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

    @app.websocket("/api/v1/browser-sessions/{session_id}/vnc")
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

    @app.get("/api/v1/browser-sessions/{session_id}/screenshot", deprecated=True)
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

    @app.post("/api/v1/browser-sessions/{session_id}/activity", status_code=204)
    async def browser_activity(session_id: str) -> None:
        try:
            await browser_sessions.mark_activity(session_id)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @app.post(
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

    @app.post(
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

    @app.post(
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

    @app.post(
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

    return app
