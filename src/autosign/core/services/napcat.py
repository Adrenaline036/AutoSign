from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus

NAPCAT_BASE_URL_SECRET = "napcat_base_url"
NAPCAT_TOKEN_SECRET = "napcat_access_token"
NAPCAT_TARGET_TYPE_SECRET = "napcat_target_type"
NAPCAT_TARGET_ID_SECRET = "napcat_target_id"
NAPCAT_SECRET_NAMES = {
    NAPCAT_BASE_URL_SECRET,
    NAPCAT_TOKEN_SECRET,
    NAPCAT_TARGET_TYPE_SECRET,
    NAPCAT_TARGET_ID_SECRET,
}


@dataclass(frozen=True, slots=True)
class NapCatDelivery:
    configured: bool
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class NapCatConfig:
    base_url: str
    access_token: str
    target_type: str
    target_id: str


class NapCatClient:
    def __init__(self, *, timeout_seconds: float = 10) -> None:
        self._timeout_seconds = timeout_seconds

    async def send(self, config: NapCatConfig, message: str) -> None:
        await asyncio.to_thread(self._send_sync, config, message)

    @staticmethod
    def validate_config(
        *,
        base_url: str,
        access_token: str,
        target_type: str,
        target_id: str,
    ) -> NapCatConfig:
        clean_url = base_url.strip().rstrip("/")
        parts = urlsplit(clean_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("NapCat API address must be a complete HTTP or HTTPS URL.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("NapCat API address must not contain credentials, query, or fragment.")
        clean_token = access_token.strip()
        if not clean_token:
            raise ValueError("NapCat OneBot access token is required.")
        if target_type not in {"private", "group"}:
            raise ValueError("NapCat target type must be private or group.")
        clean_target = target_id.strip()
        if not clean_target.isdigit() or not 5 <= len(clean_target) <= 20:
            raise ValueError("QQ user ID or group ID must contain 5-20 digits.")
        return NapCatConfig(
            base_url=clean_url,
            access_token=clean_token,
            target_type=target_type,
            target_id=clean_target,
        )

    def _send_sync(self, config: NapCatConfig, message: str) -> None:
        action = "send_private_msg" if config.target_type == "private" else "send_group_msg"
        target_key = "user_id" if config.target_type == "private" else "group_id"
        body = json.dumps(
            {
                target_key: config.target_id,
                "message": message,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(  # noqa: S310
            f"{config.base_url}/{action}",
            data=body,
            headers={
                "Authorization": f"Bearer {config.access_token}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
            payload_bytes = response.read()
            if not 200 <= response.status < 300:
                raise RuntimeError(f"NapCat returned HTTP {response.status}.")
        try:
            payload = json.loads(payload_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("NapCat returned an invalid JSON response.") from exc
        if not isinstance(payload, dict) or (
            payload.get("status") != "ok" or payload.get("retcode") != 0
        ):
            detail = payload.get("message") or payload.get("wording") or payload
            raise RuntimeError(f"NapCat rejected the message: {detail}")


class NapCatService:
    def __init__(self, vault: VaultService, client: NapCatClient | None = None) -> None:
        self._vault = vault
        self._client = client or NapCatClient()

    def configured(self, account_id: str) -> bool:
        return NAPCAT_SECRET_NAMES.issubset(self._vault.list_names(account_id))

    def configure(
        self,
        account_id: str,
        *,
        base_url: str,
        access_token: str,
        target_type: str,
        target_id: str,
    ) -> None:
        config = self._client.validate_config(
            base_url=base_url,
            access_token=access_token,
            target_type=target_type,
            target_id=target_id,
        )
        values = {
            NAPCAT_BASE_URL_SECRET: config.base_url,
            NAPCAT_TOKEN_SECRET: config.access_token,
            NAPCAT_TARGET_TYPE_SECRET: config.target_type,
            NAPCAT_TARGET_ID_SECRET: config.target_id,
        }
        for name, value in values.items():
            self._vault.set(account_id, name, value)

    def delete(self, account_id: str) -> None:
        existing = set(self._vault.list_names(account_id))
        for name in NAPCAT_SECRET_NAMES & existing:
            self._vault.delete(account_id, name)

    async def test(self, account_id: str) -> NapCatDelivery:
        return await self._deliver(account_id, "AutoSign NapCat 通知测试成功")

    async def send_result(
        self,
        account_id: str,
        *,
        account_label: str,
        plugin_id: str,
        result: SignResult,
    ) -> NapCatDelivery:
        status_text = {
            SignStatus.SUCCESS: "签到成功",
            SignStatus.ALREADY_SIGNED: "今日已签到",
            SignStatus.FAILED: "签到失败",
            SignStatus.INTERACTION_REQUIRED: "需要重新登录",
        }[result.status]
        executed_at = result.executed_at or datetime.now().astimezone()
        lines = [
            "【AutoSign 每日签到】",
            f"账户：{account_label}",
            f"站点：{plugin_id}",
            f"结果：{status_text}",
            f"时间：{executed_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
            f"详情：{result.message}",
        ]
        if result.duration_ms is not None:
            lines.append(f"耗时：{result.duration_ms} ms")
        return await self._deliver(account_id, "\n".join(lines))

    async def _deliver(self, account_id: str, message: str) -> NapCatDelivery:
        if not self.configured(account_id):
            return NapCatDelivery(False, False, "该账户尚未配置 NapCat 通知。")
        config = NapCatConfig(
            base_url=self._vault.get(account_id, NAPCAT_BASE_URL_SECRET),
            access_token=self._vault.get(account_id, NAPCAT_TOKEN_SECRET),
            target_type=self._vault.get(account_id, NAPCAT_TARGET_TYPE_SECRET),
            target_id=self._vault.get(account_id, NAPCAT_TARGET_ID_SECRET),
        )
        try:
            await self._client.send(config, message)
        except Exception as exc:
            return NapCatDelivery(True, False, f"NapCat 推送失败：{exc}")
        return NapCatDelivery(True, True, "NapCat 已发送 QQ 消息。")
