from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any

from autosign.plugin_sdk import (
    AutoSignPlugin,
    BrowserAutomation,
    PluginCapability,
    PluginContext,
    PluginManifest,
    SessionResult,
    SessionState,
    SignResult,
    SignStatus,
)


@dataclass(slots=True)
class _VikacgApiSession:
    account_cache: dict[str, Any]
    account: dict[str, Any]
    token: str
    refresh_token: str | None
    device_id: str
    client_id: str


@dataclass(frozen=True, slots=True)
class VikacgImportResult:
    token_present: bool
    refresh_token_present: bool
    token_refreshed: bool


class VikacgImportError(ValueError):
    """A safe, user-facing failure raised while importing VikACG state."""


class VikacgPlugin(AutoSignPlugin):
    """VikACG API-first daily mission connector with a browser fallback."""

    ORIGIN = "https://www.vikacg.com"
    SIGN_URL = "https://www.vikacg.com/wallet/mission"
    MISSION_API_URL = "https://www.vikacg.com/api/vikacg/v1/userMission"
    REFRESH_API_URL = "https://www.vikacg.com/api/vikacg/v1/refreshToken"
    USER_INFO_API_URL = "https://www.vikacg.com/api/vikacg/v1/getUserInfo"
    ACCOUNT_STORAGE_KEY = "accountStore3"
    PERSONA_STORAGE_KEY = "personaStore"
    SIGN_BUTTON_SELECTORS = (
        'button:has-text("立即签到")',
        '[role="button"]:has-text("立即签到")',
        'text="立即签到"',
    )
    SIGN_BUTTON_SELECTOR = SIGN_BUTTON_SELECTORS[0]
    READY_POLL_ATTEMPTS = 20
    READY_POLL_INTERVAL_SECONDS = 1.0
    CLICK_POLL_ATTEMPTS = 6
    CLICK_POLL_INTERVAL_SECONDS = 0.5
    POLL_ATTEMPTS = 6
    POLL_INTERVAL_SECONDS = 0.75
    BODY_READ_TIMEOUT_LIMIT = 3

    manifest = PluginManifest(
        id="vikacg",
        name="VikACG 维咔",
        version="0.3.2",
        description="使用已保存的登录状态优先调用 VikACG 官方接口签到，并保留页面后备流程。",
        domains=["www.vikacg.com", "vikacg.com"],
        # Start from the same page that VikACG uses for its daily mission.  Its
        # own "立即登录" entry preserves the redirect and avoids depending on
        # the less reliable standalone /sign SPA route.
        login_url=SIGN_URL,
        login_success_selectors=(
            'a[href="/message"]',
            'a[href="/account"]',
            'button:has-text("立即签到")',
        ),
        capabilities={
            PluginCapability.INTERACTIVE_LOGIN,
            PluginCapability.HTTP_SIGN,
            PluginCapability.BROWSER_SIGN,
            PluginCapability.BROWSER_FALLBACK,
        },
    )

    async def check_session(self, context: PluginContext) -> SessionResult:
        return SessionResult(
            state=SessionState.UNKNOWN,
            message="VikACG 会话将在每日签到页面中检测。",
        )

    async def sign(self, context: PluginContext) -> SignResult:
        browser = context.browser
        if browser is None:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="尚未保存 VikACG 登录状态，请先完成交互登录。",
                verified=False,
            )

        api_session = await self._load_api_session(browser)
        if api_session is not None:
            return await self._sign_via_api(browser, api_session)

        return await self._sign_via_page(browser)

    @classmethod
    def prepare_imported_storage_state(
        cls,
        storage_state_json: str,
        imported_value: str,
    ) -> tuple[str, bool, bool]:
        """Merge only imported credentials into an existing VikACG state skeleton."""
        try:
            old_state = json.loads(storage_state_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VikacgImportError("现有登录状态无法读取，请先重新完成一次交互登录。") from exc
        if not isinstance(old_state, dict):
            raise VikacgImportError("现有登录状态格式无效，请先重新完成一次交互登录。")

        imported_cache = cls._decode_import_value(imported_value)
        imported_account = cls._select_current_account(imported_cache, source="导入内容")
        imported_id = str(imported_cache.get("currentID"))
        token = imported_account.get("token")
        refresh_token = imported_account.get("refreshToken")
        token_present = isinstance(token, str) and bool(token.strip())
        refresh_present = isinstance(refresh_token, str) and bool(refresh_token.strip())
        if not token_present and not refresh_present:
            raise VikacgImportError("导入内容中没有可用的 token 或 refreshToken。")

        candidate = copy.deepcopy(old_state)
        records = cls._account_store_records(candidate)
        if len(records) != 1:
            raise VikacgImportError(
                "现有登录状态中未找到唯一的 accountStore3 记录，请先重新完成一次交互登录。"
            )
        record = records[0]
        old_cache = cls._decode_state_value(record.get("value"), source="现有登录状态")
        old_account = cls._select_current_account(old_cache, source="现有登录状态")
        if str(old_cache.get("currentID")) != imported_id:
            raise VikacgImportError("导入内容与当前 AutoSign 账户不是同一个 VikACG 账户。")

        if token_present:
            old_account["token"] = token
        if refresh_present:
            old_account["refreshToken"] = refresh_token
        record["value"] = json.dumps(
            old_cache,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":")),
            token_present,
            refresh_present,
        )

    async def validate_imported_session(
        self,
        browser: BrowserAutomation,
        *,
        force_refresh: bool = False,
    ) -> VikacgImportResult:
        """Validate imported credentials without running the daily mission."""
        session = await self._load_api_session(browser, require_token=False)
        if session is None:
            raise VikacgImportError("导入后的登录状态无法读取。")

        if session.token and not force_refresh:
            try:
                response, payload = await self._post_api(
                    browser,
                    self.USER_INFO_API_URL,
                    {"detail": True},
                    self._api_headers(session, include_auth=True),
                )
            except Exception as exc:
                raise VikacgImportError("暂时无法连接 VikACG 验证登录状态，请稍后重试。") from exc
            if self._api_succeeded(response.status, payload):
                self._verify_user_identity(session, payload)
                return VikacgImportResult(True, bool(session.refresh_token), False)
            if not self._api_unauthorized(response.status, payload):
                raise VikacgImportError("VikACG 暂时无法验证登录状态，请稍后重试。")

        if not session.refresh_token:
            raise VikacgImportError("导入的 VikACG 登录状态已经失效。")
        try:
            response, payload = await self._post_api(
                browser,
                self.REFRESH_API_URL,
                {"refreshToken": session.refresh_token},
                self._api_headers(session, include_auth=False),
            )
        except Exception as exc:
            raise VikacgImportError("暂时无法刷新 VikACG 登录状态，请稍后重试。") from exc
        if not self._api_succeeded(response.status, payload):
            if self._api_unauthorized(response.status, payload):
                raise VikacgImportError("导入的 VikACG 登录状态已经失效。")
            raise VikacgImportError("VikACG 暂时无法刷新登录状态，请稍后重试。")

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        new_token = data.get("token")
        new_refresh_token = data.get("refreshToken")
        if not isinstance(new_token, str) or not new_token:
            raise VikacgImportError("VikACG 刷新登录状态时没有返回有效令牌。")
        session.token = new_token
        session.account["token"] = new_token
        if isinstance(new_refresh_token, str) and new_refresh_token:
            session.refresh_token = new_refresh_token
            session.account["refreshToken"] = new_refresh_token

        try:
            await browser.goto(self.USER_INFO_API_URL)
            persisted = await browser.write_storage_value(
                self.ACCOUNT_STORAGE_KEY,
                json.dumps(session.account_cache, ensure_ascii=False, separators=(",", ":")),
            )
            if not persisted:
                raise VikacgImportError("刷新后的 VikACG 登录状态无法写入临时浏览器。")
            response, payload = await self._post_api(
                browser,
                self.USER_INFO_API_URL,
                {"detail": True},
                self._api_headers(session, include_auth=True),
            )
        except VikacgImportError:
            raise
        except Exception as exc:
            raise VikacgImportError("刷新后无法完成 VikACG 登录验证，请稍后重试。") from exc
        if not self._api_succeeded(response.status, payload):
            raise VikacgImportError("刷新后的 VikACG 登录状态仍然无效。")
        self._verify_user_identity(session, payload)
        return VikacgImportResult(True, bool(session.refresh_token), True)

    @staticmethod
    def _verify_user_identity(
        session: _VikacgApiSession,
        payload: dict[str, Any],
    ) -> None:
        data = payload.get("data")
        basic = data.get("basic") if isinstance(data, dict) else None
        user_id = basic.get("id") if isinstance(basic, dict) else None
        if user_id is None or str(user_id) != str(session.account.get("id")):
            raise VikacgImportError("导入令牌对应的 VikACG 账户与当前账户不一致。")

    @classmethod
    def _decode_import_value(cls, value: str) -> dict[str, Any]:
        decoded: Any = value
        for _ in range(2):
            if not isinstance(decoded, str):
                break
            try:
                decoded = json.loads(decoded)
            except json.JSONDecodeError as exc:
                raise VikacgImportError("导入内容不是完整、有效的 accountStore3 JSON。") from exc
        if not isinstance(decoded, dict):
            raise VikacgImportError("导入内容必须是 accountStore3 的完整 JSON 对象。")
        return decoded

    @classmethod
    def _decode_state_value(cls, value: Any, *, source: str) -> dict[str, Any]:
        try:
            decoded = cls._decode_storage_json(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VikacgImportError(f"{source}中的 accountStore3 格式无效。") from exc
        if decoded is None:
            raise VikacgImportError(f"{source}中的 accountStore3 格式无效。")
        return decoded

    @staticmethod
    def _select_current_account(cache: dict[str, Any], *, source: str) -> dict[str, Any]:
        accounts = cache.get("accounts")
        current_id = cache.get("currentID")
        if not isinstance(accounts, list) or current_id is None:
            raise VikacgImportError(f"{source}缺少 accounts 或 currentID。")
        matches = [
            item
            for item in accounts
            if isinstance(item, dict) and str(item.get("id")) == str(current_id)
        ]
        if len(matches) != 1:
            raise VikacgImportError(f"{source}无法唯一确定当前 VikACG 账户。")
        return matches[0]

    @classmethod
    def _account_store_records(cls, state: dict[str, Any]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for origin in state.get("origins", []):
            if not isinstance(origin, dict) or origin.get("origin") != cls.ORIGIN:
                continue
            for item in origin.get("localStorage", []):
                if (
                    isinstance(item, dict)
                    and item.get("name") == cls.ACCOUNT_STORAGE_KEY
                ):
                    records.append(item)
            for database in origin.get("indexedDB", []):
                if not isinstance(database, dict):
                    continue
                for store in database.get("stores", []):
                    if not isinstance(store, dict):
                        continue
                    for record in store.get("records", []):
                        if (
                            isinstance(record, dict)
                            and record.get("key") == cls.ACCOUNT_STORAGE_KEY
                        ):
                            records.append(record)
        return records

    async def _sign_via_api(
        self,
        browser: BrowserAutomation,
        session: _VikacgApiSession,
    ) -> SignResult:
        try:
            response, payload = await self._request_mission(browser, session)
        except Exception as exc:
            return self._api_exception_result(exc, stage="api_sign")
        if self._api_already_signed(payload, response.text):
            return self._api_already_result(response.status, payload)
        if self._api_succeeded(response.status, payload):
            return self._api_success_result(response.status, payload)

        if not self._api_unauthorized(response.status, payload):
            return self._api_failure_result(
                response.status,
                payload,
                response.text,
                stage="api_sign",
            )

        if not session.refresh_token:
            return self._api_interaction_required(response.status, payload)

        try:
            refresh_response, refresh_payload = await self._post_api(
                browser,
                self.REFRESH_API_URL,
                {"refreshToken": session.refresh_token},
                self._api_headers(session, include_auth=False),
            )
        except Exception as exc:
            return self._api_exception_result(exc, stage="refresh_token")
        if not self._api_succeeded(refresh_response.status, refresh_payload):
            if self._api_unauthorized(refresh_response.status, refresh_payload):
                return self._api_interaction_required(refresh_response.status, refresh_payload)
            return self._api_failure_result(
                refresh_response.status,
                refresh_payload,
                refresh_response.text,
                stage="refresh_token",
            )

        refresh_data = refresh_payload.get("data")
        if not isinstance(refresh_data, dict):
            refresh_data = {}
        new_token = refresh_data.get("token")
        new_refresh_token = refresh_data.get("refreshToken")
        if not isinstance(new_token, str) or not new_token:
            return SignResult(
                status=SignStatus.FAILED,
                message="VikACG 刷新登录状态后没有返回新令牌。",
                verified=False,
                details={
                    "stage": "refresh_token",
                    "http_status": refresh_response.status,
                },
            )

        session.token = new_token
        if isinstance(new_refresh_token, str) and new_refresh_token:
            session.refresh_token = new_refresh_token
        session.account["token"] = session.token
        if session.refresh_token:
            session.account["refreshToken"] = session.refresh_token

        # The API request context shares cookies but cannot modify IndexedDB.
        # Open the same-origin API route only long enough to update localForage;
        # the verified run will then be captured and encrypted by core.
        try:
            await browser.goto(self.MISSION_API_URL)
            state_persisted = await browser.write_storage_value(
                self.ACCOUNT_STORAGE_KEY,
                json.dumps(session.account_cache, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            state_persisted = False

        try:
            response, payload = await self._request_mission(browser, session)
        except Exception as exc:
            return self._api_exception_result(
                exc,
                stage="api_sign_after_refresh",
                extra_details={"token_refreshed": True, "state_persisted": state_persisted},
            )
        details = {"token_refreshed": True, "state_persisted": state_persisted}
        if self._api_already_signed(payload, response.text):
            return self._api_already_result(response.status, payload, extra_details=details)
        if self._api_succeeded(response.status, payload):
            return self._api_success_result(response.status, payload, extra_details=details)
        if self._api_unauthorized(response.status, payload):
            return self._api_interaction_required(response.status, payload, extra_details=details)
        return self._api_failure_result(
            response.status,
            payload,
            response.text,
            stage="api_sign_after_refresh",
            extra_details=details,
        )

    async def _request_mission(
        self,
        browser: BrowserAutomation,
        session: _VikacgApiSession,
    ):
        return await self._post_api(
            browser,
            self.MISSION_API_URL,
            {},
            self._api_headers(session, include_auth=True),
        )

    @staticmethod
    async def _post_api(
        browser: BrowserAutomation,
        url: str,
        data: dict[str, Any],
        headers: dict[str, str],
    ):
        response = await browser.post_json(url, data, headers=headers)
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return response, payload

    async def _load_api_session(
        self,
        browser: BrowserAutomation,
        *,
        require_token: bool = True,
    ) -> _VikacgApiSession | None:
        try:
            account_raw = await browser.storage_value(self.ORIGIN, self.ACCOUNT_STORAGE_KEY)
            persona_raw = await browser.storage_value(self.ORIGIN, self.PERSONA_STORAGE_KEY)
            account_cache = self._decode_storage_json(account_raw)
            persona_cache = self._decode_storage_json(persona_raw)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if account_cache is None:
            return None

        accounts = account_cache.get("accounts")
        current_id = account_cache.get("currentID")
        if not isinstance(accounts, list):
            return None
        current = next(
            (
                item
                for item in accounts
                if isinstance(item, dict) and str(item.get("id")) == str(current_id)
            ),
            None,
        )
        if current is None and len(accounts) == 1 and isinstance(accounts[0], dict):
            current = accounts[0]
        if current is None:
            return None
        token = current.get("token")
        if not isinstance(token, str):
            token = ""
        if require_token and not token:
            return None
        refresh_token = current.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            refresh_token = None

        device = persona_cache.get("device", {}) if persona_cache else {}
        if not isinstance(device, dict):
            device = {}
        return _VikacgApiSession(
            account_cache=account_cache,
            account=current,
            token=token,
            refresh_token=refresh_token,
            device_id=str(device.get("deviceId") or ""),
            client_id=str(device.get("clientId") or ""),
        )

    @staticmethod
    def _decode_storage_json(value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            value = json.loads(value)
        return value if isinstance(value, dict) else None

    @staticmethod
    def _api_headers(
        session: _VikacgApiSession,
        *,
        include_auth: bool,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json",
            "Origin": VikacgPlugin.ORIGIN,
            "Referer": VikacgPlugin.SIGN_URL,
            "X-Client-Name": "VikACG Moonlight",
            "Architecture": "AixPot",
            "X-Device-Code": session.device_id,
            "X-Client-Code": session.client_id,
        }
        if include_auth:
            headers["Authorization"] = f"Bearer {session.token}"
        return headers

    @staticmethod
    def _api_succeeded(http_status: int, payload: dict[str, Any]) -> bool:
        return http_status < 400 and payload.get("status") == "success"

    @classmethod
    def _api_already_signed(cls, payload: dict[str, Any], raw_text: str) -> bool:
        text = f"{payload.get('message', '')} {raw_text}".lower()
        return any(marker in text for marker in ("已经签到", "已签到", "already signed"))

    @staticmethod
    def _api_unauthorized(http_status: int, payload: dict[str, Any]) -> bool:
        return http_status == 401 or payload.get("code") == 401

    @classmethod
    def _api_success_result(
        cls,
        http_status: int,
        payload: dict[str, Any],
        *,
        extra_details: dict[str, Any] | None = None,
    ) -> SignResult:
        details = {"method": "site_api", "http_status": http_status}
        details.update(extra_details or {})
        message = cls._api_message(payload)
        if message:
            details["result_excerpt"] = cls._excerpt(message)
        return SignResult(
            status=SignStatus.SUCCESS,
            message="VikACG 签到成功。",
            verified=True,
            details=details,
        )

    @classmethod
    def _api_already_result(
        cls,
        http_status: int,
        payload: dict[str, Any],
        *,
        extra_details: dict[str, Any] | None = None,
    ) -> SignResult:
        details = {"method": "site_api", "http_status": http_status}
        details.update(extra_details or {})
        message = cls._api_message(payload)
        if message:
            details["result_excerpt"] = cls._excerpt(message)
        return SignResult(
            status=SignStatus.ALREADY_SIGNED,
            message="VikACG 今日已经签到。",
            verified=True,
            details=details,
        )

    @classmethod
    def _api_interaction_required(
        cls,
        http_status: int,
        payload: dict[str, Any],
        *,
        extra_details: dict[str, Any] | None = None,
    ) -> SignResult:
        details = {"stage": "api_auth", "http_status": http_status}
        details.update(extra_details or {})
        message = cls._api_message(payload)
        if message:
            details["result_excerpt"] = cls._excerpt(message)
        return SignResult(
            status=SignStatus.INTERACTION_REQUIRED,
            message="VikACG 登录令牌已失效，且无法自动刷新。",
            verified=False,
            details=details,
        )

    @classmethod
    def _api_failure_result(
        cls,
        http_status: int,
        payload: dict[str, Any],
        raw_text: str,
        *,
        stage: str,
        extra_details: dict[str, Any] | None = None,
    ) -> SignResult:
        details = {"stage": stage, "http_status": http_status}
        details.update(extra_details or {})
        message = cls._api_message(payload) or raw_text
        if message:
            details["result_excerpt"] = cls._excerpt(message)
        if http_status == 403 and cls._cloudflare_challenge(raw_text):
            details["stage"] = "cloudflare_challenge"
            result_message = "VikACG 接口触发了 Cloudflare 安全验证。"
        else:
            result_message = "VikACG 接口未返回可确认的签到结果。"
        return SignResult(
            status=SignStatus.FAILED,
            message=result_message,
            verified=False,
            details=details,
        )

    @classmethod
    def _api_exception_result(
        cls,
        exc: Exception,
        *,
        stage: str,
        extra_details: dict[str, Any] | None = None,
    ) -> SignResult:
        details = {"stage": stage, "error_type": type(exc).__name__}
        details.update(extra_details or {})
        return SignResult(
            status=SignStatus.FAILED,
            message="VikACG 接口请求失败。",
            verified=False,
            details=details,
        )

    @staticmethod
    def _api_message(payload: dict[str, Any]) -> str:
        message = payload.get("message")
        return message if isinstance(message, str) else ""

    @staticmethod
    def _cloudflare_challenge(text: str) -> bool:
        lowered = text.lower()
        return any(
            marker in lowered
            for marker in ("cf-mitigated", "challenge-platform", "正在进行安全验证")
        )

    @classmethod
    def _cloudflare_page_result(cls, text: str) -> SignResult:
        return SignResult(
            status=SignStatus.FAILED,
            message="VikACG 页面触发了 Cloudflare 安全验证，自动化浏览器无法继续。",
            verified=False,
            details={
                "stage": "cloudflare_challenge",
                "result_excerpt": cls._excerpt(text),
            },
        )

    async def _sign_via_page(self, browser: BrowserAutomation) -> SignResult:
        status = await browser.goto(self.SIGN_URL)
        if status is not None and status >= 400:
            return SignResult(
                status=SignStatus.FAILED,
                message=f"VikACG 签到页面返回 HTTP {status}。",
                verified=False,
                details={"http_status": status},
            )

        body = ""
        mission_ready = False
        body_read_timeouts = 0
        for attempt in range(self.READY_POLL_ATTEMPTS):
            current_body = await self._body_text_or_none(browser)
            if current_body is None:
                body_read_timeouts += 1
                if body_read_timeouts >= self.BODY_READ_TIMEOUT_LIMIT:
                    break
            else:
                body = current_body
            if self._cloudflare_challenge(body):
                return self._cloudflare_page_result(body)
            # Some SPA shells briefly render a login prompt while restoring the
            # client-side session. A visible mission state is more authoritative
            # than login-related text elsewhere in the same page.
            if self._already_signed(body) or self._can_sign(body):
                mission_ready = True
                break
            if attempt + 1 < self.READY_POLL_ATTEMPTS:
                await asyncio.sleep(self.READY_POLL_INTERVAL_SECONDS)

        if self._already_signed(body):
            return SignResult(
                status=SignStatus.ALREADY_SIGNED,
                message="VikACG 今日已经签到。",
                verified=True,
                details={"result_excerpt": self._excerpt(body)},
            )
        if not mission_ready:
            html = await browser.html_content()
            if self._cloudflare_challenge(html):
                return self._cloudflare_page_result(html)
            if not body and body_read_timeouts:
                return SignResult(
                    status=SignStatus.FAILED,
                    message="VikACG 签到页面持续切换，暂时无法读取页面内容。",
                    verified=False,
                    details={
                        "stage": "load_mission_page",
                        "body_read_timeouts": body_read_timeouts,
                    },
                )
            if self._login_required(body):
                return self._interaction_required(body)
            details: dict[str, str | int] = {
                "stage": "find_sign_button",
                "result_excerpt": self._excerpt(body),
            }
            if body_read_timeouts:
                details["body_read_timeouts"] = body_read_timeouts
            return SignResult(
                status=SignStatus.FAILED,
                message=(
                    "VikACG 页面等待超时，未找到可确认的签到状态，网站可能仍在加载或结构已经变化。"
                ),
                verified=False,
                details=details,
            )

        clicked = False
        for attempt in range(self.CLICK_POLL_ATTEMPTS):
            for selector in self.SIGN_BUTTON_SELECTORS:
                if await browser.click(selector):
                    clicked = True
                    break
            if clicked:
                break
            if attempt + 1 < self.CLICK_POLL_ATTEMPTS:
                await asyncio.sleep(self.CLICK_POLL_INTERVAL_SECONDS)

        if not clicked:
            return SignResult(
                status=SignStatus.FAILED,
                message="VikACG 的签到按钮未能点击，网站结构可能已经变化。",
                verified=False,
                details={
                    "stage": "click_sign_button",
                    "result_excerpt": self._excerpt(body),
                },
            )

        latest_body = body
        body_read_timeouts = 0
        for _attempt in range(self.POLL_ATTEMPTS):
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
            current_body = await self._body_text_or_none(browser)
            if current_body is None:
                body_read_timeouts += 1
                if body_read_timeouts >= self.BODY_READ_TIMEOUT_LIMIT:
                    break
                continue
            latest_body = current_body
            if self._sign_succeeded(latest_body):
                return SignResult(
                    status=SignStatus.SUCCESS,
                    message="VikACG 签到成功。",
                    verified=True,
                    details={"result_excerpt": self._excerpt(latest_body)},
                )
            if self._login_required(latest_body):
                return self._interaction_required(latest_body)

        details = {
            "stage": "verify_sign_result",
            "result_excerpt": self._excerpt(latest_body),
        }
        if body_read_timeouts:
            details["body_read_timeouts"] = body_read_timeouts
        return SignResult(
            status=SignStatus.FAILED,
            message=(
                "VikACG 点击签到后页面持续切换，未能读取成功状态。"
                if body_read_timeouts >= self.BODY_READ_TIMEOUT_LIMIT
                else "VikACG 点击签到后没有出现可确认的成功状态。"
            ),
            verified=False,
            details=details,
        )

    @staticmethod
    async def _body_text_or_none(browser: BrowserAutomation) -> str | None:
        """Ignore only transient Playwright timeouts while the SPA replaces ``body``."""
        try:
            return await browser.body_text()
        except Exception as exc:
            message = str(exc)
            is_body_timeout = type(exc).__name__ == "TimeoutError" and 'locator("body")' in message
            if is_body_timeout:
                return None
            raise

    @staticmethod
    def _login_required(text: str) -> bool:
        return any(
            marker in text for marker in ("请先登录", "登录后即可查看您的积分", "使用维咔账号登录")
        )

    @staticmethod
    def _already_signed(text: str) -> bool:
        return any(
            marker in text for marker in ("今日已签", "今天已签到", "今日已经签到", "已经签到")
        )

    @staticmethod
    def _can_sign(text: str) -> bool:
        return "今日未签" in text and "立即签到" in text

    @classmethod
    def _sign_succeeded(cls, text: str) -> bool:
        return cls._already_signed(text) or any(
            marker in text for marker in ("签到成功", "成功签到", "恭喜签到")
        )

    @classmethod
    def _interaction_required(cls, text: str) -> SignResult:
        return SignResult(
            status=SignStatus.INTERACTION_REQUIRED,
            message="VikACG 登录状态已失效，请重新进行交互登录。",
            verified=False,
            details={"result_excerpt": cls._excerpt(text)},
        )

    @staticmethod
    def _excerpt(text: str, *, limit: int = 500) -> str:
        return " ".join(text.split())[:limit]
