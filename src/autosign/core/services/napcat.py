from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

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
