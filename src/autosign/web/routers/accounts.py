from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException

from autosign.core.account_operations import (
    AccountOperationGate,
    AccountOperationRejectedError,
)
from autosign.core.browser_sessions import BrowserStorageStateError
from autosign.core.db import Account
from autosign.core.plugin_registry import PluginRegistry
from autosign.core.services import (
    AccountService,
    NotificationChannelService,
    ScheduleService,
    VaultService,
)
from autosign.core.services.accounts import AccountNotFoundError
from autosign.core.services.notifications import NAPCAT, UPTIME_KUMA
from autosign.plugin_sdk import SignResult
from autosign.web.errors import account_error
from autosign.web.schemas import (
    AccountCreate,
    AccountDelete,
    AccountRead,
    AccountUpdate,
    ScheduleRead,
    ScheduleWrite,
    SecretList,
    SecretWrite,
)
from autosign.web.serialization import aware_utc


def create_accounts_router(
    *,
    accounts: AccountService,
    vault: VaultService,
    registry: PluginRegistry,
    account_operations: AccountOperationGate,
    notifications: NotificationChannelService,
    schedules: ScheduleService,
    run_account: Callable[[str], Awaitable[SignResult]],
    notify_final_result: Callable[[str, SignResult], Awaitable[None]],
) -> APIRouter:
    router = APIRouter()

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

    @router.get("/api/v1/accounts", response_model=list[AccountRead])
    async def list_accounts() -> list[AccountRead]:
        return [serialize_account(account) for account in accounts.list()]

    @router.post("/api/v1/accounts", response_model=AccountRead, status_code=201)
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

    @router.get("/api/v1/accounts/{account_id}", response_model=AccountRead)
    async def get_account(account_id: str) -> AccountRead:
        try:
            return serialize_account(accounts.get(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @router.patch("/api/v1/accounts/{account_id}", response_model=AccountRead)
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

    @router.post("/api/v1/accounts/{account_id}/delete", status_code=204)
    async def delete_account(account_id: str, request: AccountDelete) -> None:
        try:
            async with account_operations.delete(account_id):
                accounts.delete(account_id, confirm_label=request.confirm_label)
        except AccountOperationRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (AccountNotFoundError, ValueError) as exc:
            raise account_error(exc) from exc

    @router.get("/api/v1/accounts/{account_id}/secrets", response_model=SecretList)
    async def list_secrets(account_id: str) -> SecretList:
        try:
            return SecretList(names=vault.list_names(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @router.put(
        "/api/v1/accounts/{account_id}/secrets/{name}",
        response_model=SecretList,
    )
    async def set_secret(account_id: str, name: str, request: SecretWrite) -> SecretList:
        if not name or len(name) > 100:
            raise HTTPException(
                status_code=400,
                detail="Secret name must be 1-100 characters.",
            )
        try:
            vault.set(account_id, name, request.value.get_secret_value())
            return SecretList(names=vault.list_names(account_id))
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    @router.delete(
        "/api/v1/accounts/{account_id}/secrets/{name}",
        response_model=SecretList,
    )
    async def delete_secret(account_id: str, name: str) -> SecretList:
        try:
            vault.delete(account_id, name)
            return SecretList(names=vault.list_names(account_id))
        except (AccountNotFoundError, LookupError) as exc:
            raise account_error(exc) from exc

    @router.post("/api/v1/accounts/{account_id}/execute", response_model=SignResult)
    async def execute_account(account_id: str) -> SignResult:
        try:
            result = await run_account(account_id)
            await notify_final_result(account_id, result)
            return result
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc
        except (AccountOperationRejectedError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except BrowserStorageStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/v1/schedules", response_model=list[ScheduleRead])
    async def list_schedules() -> list[ScheduleRead]:
        return [serialize_schedule(schedule) for schedule in schedules.list()]

    @router.put(
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

    @router.delete("/api/v1/accounts/{account_id}/schedule", status_code=204)
    async def delete_account_schedule(account_id: str) -> None:
        try:
            schedules.delete_for_account(account_id)
        except AccountNotFoundError as exc:
            raise account_error(exc) from exc

    return router
