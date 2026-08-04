from __future__ import annotations

import pytest

from autosign.plugin_sdk import PluginContext, SignStatus
from autosign.plugins.vikacg import VikacgPlugin


class FakeVikacgBrowser:
    def __init__(
        self,
        bodies: list[str],
        *,
        status: int = 200,
        click_result: bool = True,
        successful_selector: str | None = None,
    ) -> None:
        self.bodies = bodies
        self.status = status
        self.click_result = click_result
        self.successful_selector = successful_selector
        self.clicked_selectors: list[str] = []

    async def goto(self, _url: str, *, referrer: str | None = None) -> int:
        return self.status

    async def body_text(self) -> str:
        if len(self.bodies) > 1:
            return self.bodies.pop(0)
        return self.bodies[0]

    async def click(self, selector: str) -> bool:
        self.clicked_selectors.append(selector)
        if self.successful_selector is not None:
            return selector == self.successful_selector
        return self.click_result


async def _no_sleep(_seconds: float) -> None:
    return None


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
