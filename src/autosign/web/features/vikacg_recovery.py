from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr

from autosign.core.browser_sessions import (
    BROWSER_STATE_SECRET,
    BrowserSessionManager,
    BrowserStorageStateError,
)
from autosign.core.plugin_registry import PluginRegistry
from autosign.core.services import AccountService, VaultService
from autosign.core.services.accounts import AccountNotFoundError
from autosign.plugins.vikacg import VikacgImportError, VikacgPlugin

STATIC_DIR = Path(__file__).parents[1] / "static"


class VikacgStateImport(BaseModel):
    raw_json: SecretStr
    confirm_overwrite: bool = False


class VikacgStateImportRead(BaseModel):
    imported: bool
    token: bool
    refresh_token: bool
    token_refreshed: bool
    device_profile_preserved: bool


def create_vikacg_recovery_router(
    *,
    accounts: AccountService,
    registry: PluginRegistry,
    vault: VaultService,
    browser_sessions: BrowserSessionManager,
) -> APIRouter:
    router = APIRouter()

    @router.get("/assets/vikacg-recovery.js", include_in_schema=False)
    async def vikacg_recovery_script() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "vikacg_recovery.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @router.post(
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
        except AccountNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    return router
