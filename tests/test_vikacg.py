from __future__ import annotations

import json

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from autosign.plugin_sdk import (
    BrowserResponse,
    BrowserTransientReadError,
    PluginContext,
    SignStatus,
)
from autosign.plugins.vikacg import VikacgImportError, VikacgPlugin


def test_vikacg_interactive_login_starts_from_mission_page() -> None:
    assert VikacgPlugin.manifest.version == "0.3.2"
    assert VikacgPlugin.manifest.login_url == VikacgPlugin.SIGN_URL
    assert VikacgPlugin.manifest.login_url.endswith("/wallet/mission")


class FakeVikacgBrowser:
    def __init__(
        self,
        bodies: list[str | Exception],
        *,
        status: int = 200,
        click_result: bool = True,
        successful_selector: str | None = None,
        storage: dict[str, object] | None = None,
        api_responses: list[BrowserResponse] | None = None,
        write_result: bool = True,
    ) -> None:
        self.bodies = bodies
        self.status = status
        self.click_result = click_result
        self.successful_selector = successful_selector
        self.clicked_selectors: list[str] = []
        self.storage = storage or {}
        self.api_responses = api_responses or []
        self.api_requests: list[tuple[str, dict, dict[str, str]]] = []
        self.storage_writes: list[tuple[str, object]] = []
        self.write_result = write_result
        self.visited_urls: list[str] = []

    async def goto(self, _url: str, *, referrer: str | None = None) -> int:
        self.visited_urls.append(_url)
        return self.status

    async def body_text(self) -> str:
        if len(self.bodies) > 1:
            result = self.bodies.pop(0)
        else:
            result = self.bodies[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def html_content(self) -> str:
        value = self.bodies[0]
        return "" if isinstance(value, Exception) else value

    async def click(self, selector: str) -> bool:
        self.clicked_selectors.append(selector)
        if self.successful_selector is not None:
            return selector == self.successful_selector
        return self.click_result

    async def storage_value(self, _origin: str, key: str) -> object | None:
        return self.storage.get(key)

    async def write_storage_value(self, key: str, value: object) -> bool:
        self.storage_writes.append((key, value))
        return self.write_result

    async def post_json(
        self,
        url: str,
        data: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> BrowserResponse:
        self.api_requests.append((url, data, headers or {}))
        return self.api_responses.pop(0)


async def _no_sleep(_seconds: float) -> None:
    return None


def _api_storage(*, refresh_token: str | None = "refresh-token") -> dict[str, object]:
    account = {"id": 42, "token": "access-token", "refreshToken": refresh_token}
    return {
        VikacgPlugin.ACCOUNT_STORAGE_KEY: json.dumps(
            {"accounts": [account], "currentID": 42, "currentConfig": {}},
            ensure_ascii=False,
        ),
        VikacgPlugin.PERSONA_STORAGE_KEY: json.dumps(
            {"device": {"deviceId": "device-id", "clientId": "client-id"}}
        ),
    }


def _browser_state() -> str:
    return json.dumps(
        {
            "cookies": [
                {
                    "name": "cf_clearance",
                    "value": "keep-me",
                    "domain": ".vikacg.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": VikacgPlugin.ORIGIN,
                    "localStorage": [{"name": "theme", "value": "dark"}],
                    "indexedDB": [
                        {
                            "name": "localforage",
                            "version": 1,
                            "stores": [
                                {
                                    "name": "keyvaluepairs",
                                    "autoIncrement": False,
                                    "keyPath": None,
                                    "records": [
                                        {
                                            "key": VikacgPlugin.ACCOUNT_STORAGE_KEY,
                                            "value": json.dumps(
                                                {
                                                    "accounts": [
                                                        {
                                                            "id": 42,
                                                            "token": "old-token",
                                                            "refreshToken": "old-refresh",
                                                            "basic": {"name": "keep-profile"},
                                                        }
                                                    ],
                                                    "currentID": 42,
                                                    "currentConfig": {"keep": True},
                                                }
                                            ),
                                        },
                                        {
                                            "key": VikacgPlugin.PERSONA_STORAGE_KEY,
                                            "value": "keep-persona",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_vikacg_import_merges_only_credentials_into_existing_state() -> None:
    imported = json.dumps(
        {
            "accounts": [
                {
                    "id": 42,
                    "token": "new-token",
                    "refreshToken": "new-refresh",
                    "untrusted": "discard-me",
                }
            ],
            "currentID": 42,
            "untrustedTopLevel": "discard-me",
        }
    )

    candidate_json, token, refresh_token = VikacgPlugin.prepare_imported_storage_state(
        _browser_state(), json.dumps(imported)
    )

    candidate = json.loads(candidate_json)
    assert candidate["cookies"][0]["value"] == "keep-me"
    assert candidate["origins"][0]["localStorage"][0]["value"] == "dark"
    records = candidate["origins"][0]["indexedDB"][0]["stores"][0]["records"]
    cache = json.loads(records[0]["value"])
    assert cache["accounts"][0]["token"] == "new-token"
    assert cache["accounts"][0]["refreshToken"] == "new-refresh"
    assert cache["accounts"][0]["basic"] == {"name": "keep-profile"}
    assert "untrusted" not in cache["accounts"][0]
    assert records[1]["value"] == "keep-persona"
    assert token is True
    assert refresh_token is True


def test_vikacg_import_supports_current_local_storage_account_store() -> None:
    existing_cache = {
        "accounts": [
            {
                "id": 42,
                "token": "old-token",
                "refreshToken": "old-refresh",
                "basic": {"name": "keep-profile"},
            }
        ],
        "currentID": 42,
        "currentConfig": {"keep": True},
    }
    state = {
        "cookies": [],
        "origins": [
            {
                "origin": VikacgPlugin.ORIGIN,
                "localStorage": [
                    {
                        "name": VikacgPlugin.ACCOUNT_STORAGE_KEY,
                        "value": json.dumps(existing_cache),
                    }
                ],
                "indexedDB": [],
            }
        ],
    }
    imported = json.dumps(
        {
            "accounts": [
                {"id": 42, "token": "new-token", "refreshToken": "new-refresh"}
            ],
            "currentID": 42,
        }
    )

    candidate_json, token, refresh_token = VikacgPlugin.prepare_imported_storage_state(
        json.dumps(state), imported
    )

    saved = json.loads(candidate_json)["origins"][0]["localStorage"][0]
    cache = json.loads(saved["value"])
    assert cache["accounts"][0]["token"] == "new-token"
    assert cache["accounts"][0]["refreshToken"] == "new-refresh"
    assert cache["accounts"][0]["basic"] == {"name": "keep-profile"}
    assert token is True
    assert refresh_token is True


def test_vikacg_import_rejects_a_different_account() -> None:
    imported = json.dumps(
        {"accounts": [{"id": 99, "token": "new-token"}], "currentID": 99}
    )
    with pytest.raises(VikacgImportError, match="不是同一个"):
        VikacgPlugin.prepare_imported_storage_state(_browser_state(), imported)


@pytest.mark.asyncio
async def test_vikacg_import_validates_with_read_only_user_info() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=200,
                url=VikacgPlugin.USER_INFO_API_URL,
                text='{"status":"success","code":200,"data":{"basic":{"id":42}}}',
            )
        ],
    )

    result = await VikacgPlugin().validate_imported_session(browser)

    assert result.token_present is True
    assert result.refresh_token_present is True
    assert result.token_refreshed is False
    assert browser.api_requests[0][0] == VikacgPlugin.USER_INFO_API_URL
    assert browser.api_requests[0][2]["Authorization"] == "Bearer access-token"
    assert browser.visited_urls == []


@pytest.mark.asyncio
async def test_vikacg_import_rejects_token_for_another_user() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=200,
                url=VikacgPlugin.USER_INFO_API_URL,
                text='{"status":"success","data":{"basic":{"id":99}}}',
            )
        ],
    )

    with pytest.raises(VikacgImportError, match="账户不一致"):
        await VikacgPlugin().validate_imported_session(browser)


@pytest.mark.asyncio
async def test_vikacg_import_refreshes_once_then_verifies() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(status=401, url=VikacgPlugin.USER_INFO_API_URL, text='{"code":401}'),
            BrowserResponse(
                status=200,
                url=VikacgPlugin.REFRESH_API_URL,
                text='{"status":"success","data":{"token":"fresh-token","refreshToken":"fresh-refresh"}}',
            ),
            BrowserResponse(
                status=200,
                url=VikacgPlugin.USER_INFO_API_URL,
                text='{"status":"success","data":{"basic":{"id":42}}}',
            ),
        ],
    )

    result = await VikacgPlugin().validate_imported_session(browser)

    assert result.token_refreshed is True
    assert [request[0] for request in browser.api_requests] == [
        VikacgPlugin.USER_INFO_API_URL,
        VikacgPlugin.REFRESH_API_URL,
        VikacgPlugin.USER_INFO_API_URL,
    ]
    assert browser.api_requests[-1][2]["Authorization"] == "Bearer fresh-token"
    assert browser.visited_urls == [VikacgPlugin.USER_INFO_API_URL]
    assert len(browser.storage_writes) == 1


@pytest.mark.asyncio
async def test_vikacg_refresh_only_import_does_not_reuse_the_old_access_token() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=200,
                url=VikacgPlugin.REFRESH_API_URL,
                text='{"status":"success","data":{"token":"fresh-token"}}',
            ),
            BrowserResponse(
                status=200,
                url=VikacgPlugin.USER_INFO_API_URL,
                text='{"status":"success","data":{"basic":{"id":42}}}',
            ),
        ],
    )

    result = await VikacgPlugin().validate_imported_session(browser, force_refresh=True)

    assert result.token_refreshed is True
    assert browser.api_requests[0][0] == VikacgPlugin.REFRESH_API_URL
    assert browser.api_requests[1][0] == VikacgPlugin.USER_INFO_API_URL


@pytest.mark.asyncio
async def test_vikacg_uses_site_api_without_loading_spa() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=200,
                url=VikacgPlugin.MISSION_API_URL,
                text='{"status":"success","code":200,"message":"签到成功"}',
            )
        ],
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert result.details["method"] == "site_api"
    assert browser.visited_urls == []
    assert browser.clicked_selectors == []
    assert browser.api_requests[0][2]["Authorization"] == "Bearer access-token"
    assert browser.api_requests[0][2]["X-Device-Code"] == "device-id"


@pytest.mark.asyncio
async def test_vikacg_api_recognizes_already_signed() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=400,
                url=VikacgPlugin.MISSION_API_URL,
                text='{"status":"fail","code":400,"message":"今日已经签到"}',
            )
        ],
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert result.details["method"] == "site_api"


@pytest.mark.asyncio
async def test_vikacg_refreshes_token_persists_state_and_retries() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(),
        api_responses=[
            BrowserResponse(
                status=401,
                url=VikacgPlugin.MISSION_API_URL,
                text='{"status":"fail","code":401,"message":"需要认证令牌"}',
            ),
            BrowserResponse(
                status=200,
                url=VikacgPlugin.REFRESH_API_URL,
                text=(
                    '{"status":"success","code":200,"data":'
                    '{"token":"new-token","refreshToken":"new-refresh"}}'
                ),
            ),
            BrowserResponse(
                status=200,
                url=VikacgPlugin.MISSION_API_URL,
                text='{"status":"success","code":200,"message":"签到成功"}',
            ),
        ],
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert result.details["token_refreshed"] is True
    assert result.details["state_persisted"] is True
    assert browser.api_requests[1][0] == VikacgPlugin.REFRESH_API_URL
    assert "Authorization" not in browser.api_requests[1][2]
    assert browser.api_requests[2][2]["Authorization"] == "Bearer new-token"
    assert browser.visited_urls == [VikacgPlugin.MISSION_API_URL]
    saved_cache = json.loads(browser.storage_writes[0][1])
    assert saved_cache["accounts"][0]["token"] == "new-token"
    assert saved_cache["accounts"][0]["refreshToken"] == "new-refresh"


@pytest.mark.asyncio
async def test_vikacg_api_requires_login_when_refresh_token_is_missing() -> None:
    browser = FakeVikacgBrowser(
        ["unused"],
        storage=_api_storage(refresh_token=None),
        api_responses=[
            BrowserResponse(
                status=401,
                url=VikacgPlugin.MISSION_API_URL,
                text='{"status":"fail","code":401,"message":"需要认证令牌"}',
            )
        ],
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False
    assert result.details["stage"] == "api_auth"


@pytest.mark.asyncio
async def test_vikacg_page_fallback_reports_cloudflare_challenge(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(["请稍候… 正在进行安全验证"])

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "cloudflare_challenge"


@pytest.mark.asyncio
async def test_vikacg_signs_and_verifies_updated_page(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            "积分与签到 今日未签 立即签到",
            "签到成功 今日已签 连续签到2天",
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert browser.clicked_selectors == [VikacgPlugin.SIGN_BUTTON_SELECTOR]


@pytest.mark.asyncio
async def test_vikacg_recognizes_already_signed() -> None:
    browser = FakeVikacgBrowser(["积分与签到 今日已签 连续签到2天"])

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert browser.clicked_selectors == []


@pytest.mark.asyncio
async def test_vikacg_recognizes_plain_already_signed_label() -> None:
    browser = FakeVikacgBrowser(["积分与签到 1925 积分 获得502积分 连续签到3天 已经签到"])

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert browser.clicked_selectors == []


@pytest.mark.asyncio
async def test_vikacg_requests_login_when_session_expired(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(["请先登录 登录后即可查看您的积分总量"])

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False


@pytest.mark.asyncio
async def test_vikacg_waits_for_spa_session_restore(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            "请先登录 正在加载账户信息",
            "请先登录 正在加载账户信息",
            "积分与签到 今日已签 连续签到2天",
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True


@pytest.mark.asyncio
async def test_vikacg_prefers_mission_state_over_login_component(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            "使用维咔账号登录 积分与签到 今日未签 立即签到",
            "使用维咔账号登录 签到成功 今日已签",
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True


@pytest.mark.asyncio
async def test_vikacg_fails_when_button_cannot_be_clicked(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        ["积分与签到 今日未签 立即签到"],
        click_result=False,
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "click_sign_button"
    assert result.details["result_excerpt"] == "积分与签到 今日未签 立即签到"


@pytest.mark.asyncio
async def test_vikacg_clicks_role_button_fallback(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    role_selector = '[role="button"]:has-text("立即签到")'
    browser = FakeVikacgBrowser(
        ["积分与签到 今日未签 立即签到", "签到成功 今日已签"],
        successful_selector=role_selector,
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert browser.clicked_selectors == [
        VikacgPlugin.SIGN_BUTTON_SELECTORS[0],
        role_selector,
    ]


@pytest.mark.asyncio
async def test_vikacg_rejects_unrecognized_page(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(["积分与签到 页面维护中"])

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "find_sign_button"


@pytest.mark.asyncio
async def test_vikacg_tolerates_transient_body_timeouts(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    body_timeout = BrowserTransientReadError(
        "The page replaced its body before it could be read."
    )
    browser = FakeVikacgBrowser(
        [
            body_timeout,
            "积分与签到 今日未签 立即签到",
            body_timeout,
            "签到成功 今日已签",
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True


@pytest.mark.asyncio
async def test_vikacg_reports_repeated_body_timeouts_as_load_failure(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            BrowserTransientReadError(
                "The page replaced its body before it could be read."
            )
            for _ in range(VikacgPlugin.BODY_READ_TIMEOUT_LIMIT)
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.details == {
        "stage": "load_mission_page",
        "body_read_timeouts": VikacgPlugin.BODY_READ_TIMEOUT_LIMIT,
    }


@pytest.mark.asyncio
async def test_vikacg_reports_repeated_body_timeouts_after_click(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            "积分与签到 今日未签 立即签到",
            *[
                BrowserTransientReadError(
                    "The page replaced its body before it could be read."
                )
                for _ in range(VikacgPlugin.BODY_READ_TIMEOUT_LIMIT)
            ],
        ]
    )

    result = await VikacgPlugin().sign(
        PluginContext(account_id="a1", account_label="VikACG", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "verify_sign_result"
    assert result.details["body_read_timeouts"] == VikacgPlugin.BODY_READ_TIMEOUT_LIMIT


@pytest.mark.asyncio
async def test_vikacg_does_not_hide_non_timeout_browser_errors(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser([RuntimeError("browser target closed")])

    with pytest.raises(RuntimeError, match="browser target closed"):
        await VikacgPlugin().sign(
            PluginContext(account_id="a1", account_label="VikACG", browser=browser)
        )


@pytest.mark.asyncio
async def test_vikacg_does_not_hide_unrelated_playwright_timeouts(monkeypatch) -> None:
    monkeypatch.setattr("autosign.plugins.vikacg.asyncio.sleep", _no_sleep)
    browser = FakeVikacgBrowser(
        [
            PlaywrightTimeoutError(
                'Locator.click: Timeout 5000ms exceeded. waiting for locator("button")'
            )
        ]
    )

    with pytest.raises(PlaywrightTimeoutError, match="Locator.click"):
        await VikacgPlugin().sign(
            PluginContext(account_id="a1", account_label="VikACG", browser=browser)
        )
