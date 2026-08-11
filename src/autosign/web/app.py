from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from autosign import __version__
from autosign.core.auth import (
    SESSION_COOKIE_NAME,
    AdminAuthService,
    AuthConfigurationError,
)
from autosign.core.backup import BackupError
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
from autosign.core.db import Account, Database
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
from autosign.core.services.notifications import (
    NAPCAT,
    UPTIME_KUMA,
    NotificationChannelNotFoundError,
)
from autosign.plugin_sdk import PluginCapability, PluginManifest, SignResult
from autosign.plugins.vikacg import VikacgImportError, VikacgPlugin
from autosign.web.schemas import (
    AccountCreate,
    AccountDelete,
    AccountRead,
    AccountUpdate,
    AdminPasswordRequest,
    AuthStatus,
    BackupActionRead,
    BackupSettingsWrite,
    BackupStatusRead,
    BrowserClick,
    BrowserKeyInput,
    BrowserSessionClose,
    BrowserSessionCloseResult,
    BrowserSessionRead,
    BrowserTextInput,
    ExecutionRead,
    NotificationChannelAssignmentWrite,
    NotificationChannelDeliveryRead,
    NotificationChannelRead,
    NotificationChannelWrite,
    ScheduleRead,
    ScheduleWrite,
    SecretList,
    SecretWrite,
    VikacgStateImport,
    VikacgStateImportRead,
)

STATIC_DIR = Path(__file__).with_name("static")


class SignExecutionRequest(BaseModel):
    account_id: str = Field(default="demo-account", min_length=1, max_length=100)
    account_label: str = Field(default="演示账户", min_length=1, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)


def create_app(
    settings_override: Settings | None = None,
    browser_manager_override: BrowserSessionManager | None = None,
) -> FastAPI:
    settings = settings_override or get_settings()
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
    }
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
    browser_cleanup = BrowserSessionCleanupCoordinator(
        browser_sessions,
        poll_seconds=settings.browser_session_cleanup_poll_seconds,
    )
    executions = ExecutionService(database, runner)
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
                refreshed_state = await browser_sessions.capture_automation_state(browser)
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
    app.state.browser_cleanup = browser_cleanup
    app.state.executions = executions
    app.state.schedules = schedules
    app.state.scheduler = scheduler
    app.state.notifications = notifications
    app.state.backups = backups
    app.state.backup_coordinator = backup_coordinator
    failed_logins: dict[str, list[float]] = {}

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

    def serialize_account(account: Account) -> AccountRead:
        assigned_channels = notifications.assigned_to_account(account.id)
        channel_types = {channel.channel_type for channel in assigned_channels}
        return AccountRead(
            id=account.id,
            plugin_id=account.plugin_id,
            label=account.label,
            enabled=account.enabled,
            settings=account.settings_json,
            secret_names=vault.list_names(account.id),
            monitor_configured=UPTIME_KUMA in channel_types,
            napcat_configured=NAPCAT in channel_types,
            notification_channel_ids=[channel.id for channel in assigned_channels],
            created_at=aware_utc(account.created_at),
            updated_at=aware_utc(account.updated_at),
        )

    def account_error(exc: Exception) -> HTTPException:
        if isinstance(exc, AccountNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def notification_error(exc: Exception) -> HTTPException:
        if isinstance(exc, NotificationChannelNotFoundError):
            return HTTPException(status_code=404, detail="Unknown notification channel.")
        return account_error(exc)

    def aware_utc(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    def serialize_notification_channel(channel) -> NotificationChannelRead:
        return NotificationChannelRead(
            id=channel.id,
            name=channel.name,
            channel_type=channel.channel_type,
            assigned_account_ids=channel.assigned_account_ids,
            created_at=aware_utc(channel.created_at),
            updated_at=aware_utc(channel.updated_at),
        )

    def notification_config(request: NotificationChannelWrite) -> dict[str, str] | None:
        if request.channel_type == UPTIME_KUMA:
            if request.push_url is None:
                return None
            return {"push_url": request.push_url.get_secret_value()}
        if (
            request.base_url is None
            or request.access_token is None
            or request.target_type is None
            or request.target_id is None
        ):
            return None
        return {
            "base_url": request.base_url,
            "access_token": request.access_token.get_secret_value(),
            "target_type": request.target_type,
            "target_id": request.target_id,
        }

    def serialize_schedule(schedule) -> ScheduleRead:
        return ScheduleRead(
            id=schedule.id,
            account_id=schedule.account_id,
            account_label=schedule.account_label,
            enabled=schedule.enabled,
            daily_time=schedule.daily_time,
            timezone=schedule.timezone,
            jitter_minutes=schedule.jitter_minutes,
            max_retries=schedule.max_retries,
            retry_delay_minutes=schedule.retry_delay_minutes,
            next_run_at=aware_utc(schedule.next_run_at),
            last_run_at=aware_utc(schedule.last_run_at),
            last_status=schedule.last_status,
        )

    def serialize_backup_status() -> BackupStatusRead:
        status = backups.status()
        return BackupStatusRead(
            enabled=status.enabled,
            configured=status.configured,
            daily_time=status.daily_time,
            timezone=status.timezone,
            retention_count=status.retention_count,
            next_run_at=aware_utc(status.next_run_at),
            last_attempt_at=aware_utc(status.last_attempt_at),
            last_success_at=aware_utc(status.last_success_at),
            last_failure_at=aware_utc(status.last_failure_at),
            last_error=status.last_error,
            latest_backup_name=status.latest_backup_name,
            latest_backup_size=status.latest_backup_size,
        )

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
        client_key = request.client.host if request.client else "unknown"
        now = monotonic()
        attempts = [stamp for stamp in failed_logins.get(client_key, []) if now - stamp < 60]
        failed_logins[client_key] = attempts
        if len(attempts) >= 5:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts. Try again in one minute.",
            )
        if not auth.verify_password(request_data.password.get_secret_value()):
            attempts.append(now)
            raise HTTPException(status_code=401, detail="Administrator password is incorrect.")
        failed_logins.pop(client_key, None)
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

    @app.get("/api/v1/plugins", response_model=list[PluginManifest])
    async def list_plugins() -> list[PluginManifest]:
        return [plugin.manifest for plugin in registry.all()]

    @app.get("/api/v1/backups/status", response_model=BackupStatusRead)
    async def backup_status() -> BackupStatusRead:
        return serialize_backup_status()

    @app.post("/api/v1/backups/run", response_model=BackupActionRead)
    async def run_backup() -> BackupActionRead:
        try:
            destination = await backups.create_now()
        except BackupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return BackupActionRead(
            success=True,
            message=f"Encrypted backup created: {destination.name}",
            status=serialize_backup_status(),
        )

    @app.post("/api/v1/backups/check-latest", response_model=BackupActionRead)
    async def check_latest_backup() -> BackupActionRead:
        try:
            destination = await backups.check_latest()
        except BackupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return BackupActionRead(
            success=True,
            message=f"Backup is valid: {destination.name}",
            status=serialize_backup_status(),
        )

    @app.put("/api/v1/backups/settings", response_model=BackupStatusRead)
    async def update_backup_settings(request: BackupSettingsWrite) -> BackupStatusRead:
        try:
            await backups.update_settings(
                enabled=request.enabled,
                daily_time=request.daily_time,
                timezone=request.timezone,
                retention_count=request.retention_count,
                password=(
                    request.password.get_secret_value()
                    if request.password is not None
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return serialize_backup_status()

    @app.get("/api/v1/accounts", response_model=list[AccountRead])
    async def list_accounts() -> list[AccountRead]:
        return [serialize_account(account) for account in accounts.list()]

    @app.post("/api/v1/accounts", response_model=AccountRead, status_code=201)
    async def create_account(request: AccountCreate) -> AccountRead:
        try:
            registry.get(request.plugin_id)
        except LookupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        account = accounts.create(
            plugin_id=request.plugin_id,
            label=request.label,
            enabled=request.enabled,
            settings=request.settings,
        )
        return serialize_account(account)

    @app.get("/api/v1/accounts/{account_id}", response_model=AccountRead)
    async def get_account(account_id: str) -> AccountRead:
        try:
            return serialize_account(accounts.get(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @app.patch("/api/v1/accounts/{account_id}", response_model=AccountRead)
    async def update_account(account_id: str, request: AccountUpdate) -> AccountRead:
        try:
            account = accounts.update(
                account_id,
                label=request.label,
                enabled=request.enabled,
                settings=request.settings,
            )
            return serialize_account(account)
        except (AccountNotFoundError, ValueError) as exc:
            raise account_error(exc) from exc

    @app.post("/api/v1/accounts/{account_id}/delete", status_code=204)
    async def delete_account(account_id: str, request: AccountDelete) -> None:
        try:
            accounts.delete(account_id, confirm_label=request.confirm_label)
        except (AccountNotFoundError, ValueError) as exc:
            raise account_error(exc) from exc

    @app.get("/api/v1/accounts/{account_id}/secrets", response_model=SecretList)
    async def list_secrets(account_id: str) -> SecretList:
        try:
            return SecretList(names=vault.list_names(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @app.put(
        "/api/v1/accounts/{account_id}/secrets/{name}",
        response_model=SecretList,
    )
    async def set_secret(account_id: str, name: str, request: SecretWrite) -> SecretList:
        if not name or len(name) > 100:
            raise HTTPException(status_code=400, detail="Secret name must be 1-100 characters.")
        try:
            vault.set(account_id, name, request.value.get_secret_value())
            return SecretList(names=vault.list_names(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @app.delete(
        "/api/v1/accounts/{account_id}/secrets/{name}",
        response_model=SecretList,
    )
    async def delete_secret(account_id: str, name: str) -> SecretList:
        try:
            vault.delete(account_id, name)
            return SecretList(names=vault.list_names(account_id))
        except (AccountNotFoundError, LookupError) as exc:
            raise account_error(exc) from exc

    @app.post(
        "/api/v1/accounts/{account_id}/vikacg-state-import",
        response_model=VikacgStateImportRead,
    )
    async def import_vikacg_state(
        account_id: str,
        request: VikacgStateImport,
    ) -> VikacgStateImportRead:
        try:
            account = accounts.get(account_id)
            plugin = registry.get(account.plugin_id)
        except (AccountNotFoundError, LookupError) as exc:
            raise account_error(exc) from exc
        if account.plugin_id != "vikacg" or not isinstance(plugin, VikacgPlugin):
            raise HTTPException(status_code=400, detail="此功能只支持 VikACG 账户。")
        try:
            old_state = vault.get(account.id, BROWSER_STATE_SECRET)
        except LookupError as exc:
            raise HTTPException(
                status_code=409,
                detail="此账户还没有基础登录状态，请先完成一次交互登录。",
            ) from exc
        if not request.confirm_overwrite:
            raise HTTPException(
                status_code=409,
                detail="导入会覆盖当前 VikACG 令牌；请确认后再次提交。",
            )

        raw_json = request.raw_json.get_secret_value()
        if not raw_json.strip():
            raise HTTPException(status_code=400, detail="请粘贴完整的 accountStore3 内容。")
        if len(raw_json) > 65_536:
            raise HTTPException(status_code=413, detail="accountStore3 内容超过 65536 字符。")
        try:
            candidate_state, token_present, refresh_present = (
                plugin.prepare_imported_storage_state(old_state, raw_json)
            )
            async with browser_sessions.automation(
                storage_state_json=candidate_state,
            ) as browser:
                validation = await plugin.validate_imported_session(
                    browser,
                    force_refresh=not token_present and refresh_present,
                )
                verified_state = await browser_sessions.capture_automation_state(browser)
        except VikacgImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BrowserStorageStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"VikACG 登录状态验证失败：{type(exc).__name__}",
            ) from exc

        vault.set(account.id, BROWSER_STATE_SECRET, verified_state)
        return VikacgStateImportRead(
            imported=True,
            token=token_present and validation.token_present,
            refresh_token=refresh_present or validation.refresh_token_present,
            token_refreshed=validation.token_refreshed,
            device_profile_preserved=True,
        )

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

    @app.post(
        "/api/v1/accounts/{account_id}/browser-session",
        response_model=BrowserSessionRead,
    )
    async def start_browser_session(
        account_id: str,
        request: Request,
        clean: bool = Query(
            False,
            description="Start without restoring the account's saved browser state.",
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
        storage_state_json = None
        if not clean and BROWSER_STATE_SECRET in vault.list_names(account.id):
            storage_state_json = vault.get(account.id, BROWSER_STATE_SECRET)
        try:
            info = await browser_sessions.start(
                account_id=account.id,
                login_url=login_url,
                storage_state_json=storage_state_json,
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

    @app.get("/api/v1/browser-sessions/{session_id}/screenshot")
    async def browser_screenshot(session_id: str) -> Response:
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

    @app.post("/api/v1/browser-sessions/{session_id}/click", status_code=204)
    async def browser_click(session_id: str, request: BrowserClick) -> None:
        try:
            await browser_sessions.click(session_id, x=request.x, y=request.y)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @app.post("/api/v1/browser-sessions/{session_id}/type", status_code=204)
    async def browser_type_text(session_id: str, request: BrowserTextInput) -> None:
        try:
            await browser_sessions.type_text(session_id, text=request.text)
        except (BrowserSessionNotFoundError, BrowserSessionInputError) as exc:
            raise browser_error(exc) from exc

    @app.post("/api/v1/browser-sessions/{session_id}/press", status_code=204)
    async def browser_press_key(session_id: str, request: BrowserKeyInput) -> None:
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

    @app.post("/api/v1/accounts/{account_id}/execute", response_model=SignResult)
    async def execute_account(account_id: str) -> SignResult:
        try:
            result = await run_account(account_id)
            await notify_final_result(account_id, result)
            return result
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BrowserStorageStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/schedules", response_model=list[ScheduleRead])
    async def list_schedules() -> list[ScheduleRead]:
        return [serialize_schedule(schedule) for schedule in schedules.list()]

    @app.put(
        "/api/v1/accounts/{account_id}/schedule",
        response_model=ScheduleRead,
    )
    async def set_account_schedule(
        account_id: str,
        request: ScheduleWrite,
    ) -> ScheduleRead:
        try:
            schedule = schedules.upsert(
                account_id,
                enabled=request.enabled,
                daily_time=request.daily_time,
                timezone=request.timezone,
                jitter_minutes=request.jitter_minutes,
                max_retries=request.max_retries,
                retry_delay_minutes=request.retry_delay_minutes,
            )
            return serialize_schedule(schedule)
        except (AccountNotFoundError, ValueError) as exc:
            raise account_error(exc) from exc

    @app.delete("/api/v1/accounts/{account_id}/schedule", status_code=204)
    async def delete_account_schedule(account_id: str) -> None:
        try:
            schedules.delete_for_account(account_id)
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @app.get(
        "/api/v1/notification-channels",
        response_model=list[NotificationChannelRead],
    )
    async def list_notification_channels() -> list[NotificationChannelRead]:
        return [
            serialize_notification_channel(channel)
            for channel in notifications.list()
        ]

    @app.post(
        "/api/v1/notification-channels",
        response_model=NotificationChannelRead,
        status_code=201,
    )
    async def create_notification_channel(
        request: NotificationChannelWrite,
    ) -> NotificationChannelRead:
        config = notification_config(request)
        if config is None:
            raise HTTPException(status_code=400, detail="推送渠道配置未填写完整。")
        try:
            channel = notifications.create(
                name=request.name,
                channel_type=request.channel_type,
                config=config,
            )
            return serialize_notification_channel(channel)
        except ValueError as exc:
            raise notification_error(exc) from exc

    @app.put(
        "/api/v1/notification-channels/{channel_id}",
        response_model=NotificationChannelRead,
    )
    async def update_notification_channel(
        channel_id: str,
        request: NotificationChannelWrite,
    ) -> NotificationChannelRead:
        try:
            existing = notifications.get(channel_id)
            if existing.channel_type != request.channel_type:
                raise ValueError("推送渠道类型不能修改，请新建另一个渠道。")
            channel = notifications.update(
                channel_id,
                name=request.name,
                config=notification_config(request),
            )
            return serialize_notification_channel(channel)
        except (NotificationChannelNotFoundError, ValueError) as exc:
            raise notification_error(exc) from exc

    @app.delete("/api/v1/notification-channels/{channel_id}", status_code=204)
    async def delete_notification_channel(channel_id: str) -> None:
        try:
            notifications.delete(channel_id)
        except NotificationChannelNotFoundError as exc:
            raise notification_error(exc) from exc

    @app.post(
        "/api/v1/notification-channels/{channel_id}/test",
        response_model=NotificationChannelDeliveryRead,
    )
    async def test_notification_channel(
        channel_id: str,
    ) -> NotificationChannelDeliveryRead:
        try:
            delivery = await notifications.test(channel_id)
            if not delivery.success:
                raise HTTPException(status_code=502, detail=delivery.message)
            return NotificationChannelDeliveryRead(
                channel_id=delivery.channel_id,
                channel_name=delivery.channel_name,
                channel_type=delivery.channel_type,
                success=delivery.success,
                message=delivery.message,
            )
        except NotificationChannelNotFoundError as exc:
            raise notification_error(exc) from exc

    @app.put(
        "/api/v1/accounts/{account_id}/notification-channels",
        response_model=list[NotificationChannelRead],
    )
    async def assign_notification_channels(
        account_id: str,
        request: NotificationChannelAssignmentWrite,
    ) -> list[NotificationChannelRead]:
        try:
            return [
                serialize_notification_channel(channel)
                for channel in notifications.assign(account_id, request.channel_ids)
            ]
        except (
            AccountNotFoundError,
            NotificationChannelNotFoundError,
        ) as exc:
            raise notification_error(exc) from exc

    @app.get("/api/v1/executions", response_model=list[ExecutionRead])
    async def list_executions(
        account_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[ExecutionRead]:
        return [
            ExecutionRead(
                id=record.id,
                account_id=record.account_id,
                account_label=record.account_label,
                plugin_id=record.plugin_id,
                status=record.status,
                message=record.message,
                verified=record.verified,
                started_at=aware_utc(record.started_at),
                finished_at=aware_utc(record.finished_at),
                duration_ms=record.duration_ms,
                details=record.details,
            )
            for record in executions.list(account_id=account_id, limit=limit)
        ]

    @app.post("/api/v1/plugins/{plugin_id}/execute", response_model=SignResult)
    async def execute_plugin(plugin_id: str, request: SignExecutionRequest) -> SignResult:
        try:
            return await runner.execute(
                plugin_id,
                account_id=request.account_id,
                account_label=request.account_label,
                settings=request.settings,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
