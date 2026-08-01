from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus

UPTIME_KUMA_PUSH_URL_SECRET = "uptime_kuma_push_url"


class HttpResponse(Protocol):
    status: int

    def read(self) -> bytes:
        ...

    def __enter__(self) -> HttpResponse:
        ...

    def __exit__(self, *_args) -> None:
        ...


@dataclass(frozen=True, slots=True)
class MonitorDelivery:
    configured: bool
    success: bool
    message: str


class UptimeKumaPushClient:
    def __init__(self, *, timeout_seconds: float = 10) -> None:
        self._timeout_seconds = timeout_seconds

    async def push(
        self,
        push_url: str,
        *,
        status: str,
        message: str,
        ping_ms: int | None = None,
    ) -> None:
        url = self.build_url(
            push_url,
            status=status,
            message=message,
            ping_ms=ping_ms,
        )
        await asyncio.to_thread(self._request, url)

    @staticmethod
    def validate_url(push_url: str) -> str:
        value = push_url.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("Push URL must be a complete HTTP or HTTPS URL.")
        if parts.username or parts.password:
            raise ValueError("Push URL must not contain embedded HTTP credentials.")
        path_parts = [part for part in parts.path.split("/") if part]
        try:
            push_index = path_parts.index("push")
        except ValueError as exc:
            raise ValueError("Push URL path must contain /api/push/<token>.") from exc
        if push_index == 0 or path_parts[push_index - 1] != "api":
            raise ValueError("Push URL path must contain /api/push/<token>.")
        if len(path_parts) <= push_index + 1 or not path_parts[push_index + 1]:
            raise ValueError("Push URL is missing its monitor token.")
        return value

    @classmethod
    def build_url(
        cls,
        push_url: str,
        *,
        status: str,
        message: str,
        ping_ms: int | None,
    ) -> str:
        parts = urlsplit(cls.validate_url(push_url))
        query = {
            key: value
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key not in {"status", "msg", "ping"}
        }
        query["status"] = status
        query["msg"] = message[:250]
        if ping_ms is not None:
            query["ping"] = str(max(0, ping_ms))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def _request(self, url: str) -> None:
        with urlopen(url, timeout=self._timeout_seconds) as response:  # noqa: S310
            body = response.read()
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Uptime Kuma returned HTTP {response.status}.")
        if body:
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            if isinstance(payload, dict) and payload.get("ok") is False:
                raise RuntimeError(str(payload.get("msg") or "Uptime Kuma rejected the push."))


class MonitoringService:
    def __init__(
        self,
        vault: VaultService,
        client: UptimeKumaPushClient | None = None,
    ) -> None:
        self._vault = vault
        self._client = client or UptimeKumaPushClient()

    def configured(self, account_id: str) -> bool:
        return UPTIME_KUMA_PUSH_URL_SECRET in self._vault.list_names(account_id)

    def set_push_url(self, account_id: str, push_url: str) -> None:
        validated = self._client.validate_url(push_url)
        self._vault.set(account_id, UPTIME_KUMA_PUSH_URL_SECRET, validated)

    def delete_push_url(self, account_id: str) -> None:
        if self.configured(account_id):
            self._vault.delete(account_id, UPTIME_KUMA_PUSH_URL_SECRET)

    async def test(self, account_id: str) -> MonitorDelivery:
        return await self._deliver(
            account_id,
            status="up",
            message="AutoSign 监控连接测试成功",
            ping_ms=0,
        )

    async def send_result(self, account_id: str, result: SignResult) -> MonitorDelivery:
        is_up = result.status in {SignStatus.SUCCESS, SignStatus.ALREADY_SIGNED}
        status = "up" if is_up else "down"
        message = f"{result.status.value}: {result.message}"
        return await self._deliver(
            account_id,
            status=status,
            message=message,
            ping_ms=result.duration_ms,
        )

    async def _deliver(
        self,
        account_id: str,
        *,
        status: str,
        message: str,
        ping_ms: int | None,
    ) -> MonitorDelivery:
        try:
            push_url = self._vault.get(account_id, UPTIME_KUMA_PUSH_URL_SECRET)
        except LookupError:
            return MonitorDelivery(
                configured=False,
                success=False,
                message="该账户尚未配置 Uptime Kuma Push URL。",
            )
        try:
            await self._client.push(
                push_url,
                status=status,
                message=message,
                ping_ms=ping_ms,
            )
        except Exception as exc:
            return MonitorDelivery(
                configured=True,
                success=False,
                message=f"推送失败：{exc}",
            )
        return MonitorDelivery(
            configured=True,
            success=True,
            message="Uptime Kuma 已接受推送。",
        )
