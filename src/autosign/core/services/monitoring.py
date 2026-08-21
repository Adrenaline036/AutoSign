from __future__ import annotations

import asyncio
import json
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

UPTIME_KUMA_PUSH_URL_SECRET = "uptime_kuma_push_url"


class HttpResponse(Protocol):
    status: int

    def read(self) -> bytes:
        ...

    def __enter__(self) -> HttpResponse:
        ...

    def __exit__(self, *_args) -> None:
        ...


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
