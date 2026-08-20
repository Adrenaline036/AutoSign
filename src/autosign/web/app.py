from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
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
    BrowserSessionManager,
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
from autosign.core.services.backups import BackupCoordinator, BackupService
from autosign.plugin_sdk import PluginCapability, PluginManifest, SignResult
from autosign.web.features.vikacg_recovery import create_vikacg_recovery_router
from autosign.web.routers.accounts import create_accounts_router
from autosign.web.routers.backups import create_backups_router
from autosign.web.routers.browser import create_browser_router
from autosign.web.routers.executions import create_executions_router
from autosign.web.routers.notifications import create_notifications_router
from autosign.web.schemas import (
    AdminPasswordRequest,
    AuthStatus,
    BrowserCapacityRead,
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
    app.include_router(
        create_browser_router(
            settings=settings,
            auth=auth,
            accounts=accounts,
            registry=registry,
            vault=vault,
            browser_sessions=browser_sessions,
            screenshot_interaction_enabled=screenshot_interaction_enabled,
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

    return app
