from __future__ import annotations

import pytest

from autosign.plugin_sdk import BrowserResponse, PluginContext, SignStatus
from autosign.plugins.acgrip import AcgripPlugin


class FakeAcgripBrowser:
    def __init__(
        self,
        *,
        logged_in: bool = True,
        formhash: str | None = "form-token",
        body: str = "今天签到了吗？",
        response_text: str = "签到成功，获得 2 枚VC币",
        response_status: int = 200,
        page_status: int = 200,
    ) -> None:
        self.logged_in = logged_in
        self.formhash = formhash
        self.body = body
        self.response_text = response_text
        self.response_status = response_status
        self.page_status = page_status
        self.visits: list[str] = []
        self.submissions: list[tuple[str, dict[str, str]]] = []

    async def goto(self, url: str, *, referrer: str | None = None) -> int:
        self.visits.append(url)
        return self.page_status

    async def input_value(self, _selector: str) -> str | None:
        return self.formhash

    async def text_content(self, selector: str) -> str | None:
        if "logout" in selector and self.logged_in:
            return "退出"
        return None

    async def body_text(self) -> str:
        return self.body

    async def post_form(
        self,
        url: str,
        data: dict[str, str],
    ) -> BrowserResponse:
        self.submissions.append((url, data))
        return BrowserResponse(
            status=self.response_status,
            url=url,
            text=self.response_text,
        )


@pytest.mark.asyncio
async def test_acgrip_sign_success() -> None:
    browser = FakeAcgripBrowser(response_text="<root><![CDATA[签到成功，获得 2 枚VC币]]></root>")

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert browser.visits == [AcgripPlugin.SIGN_URL]
    assert browser.submissions == [
        (
            AcgripPlugin.SUBMIT_URL,
            {
                "formhash": "form-token",
                "qdxq": "kx",
                "qdmode": "3",
                "todaysay": "",
                "fastreply": "0",
            },
        )
    ]


@pytest.mark.asyncio
async def test_acgrip_already_signed_does_not_submit() -> None:
    browser = FakeAcgripBrowser(
        formhash=None,
        body="您今天已经签到过了，签到排名 25",
    )

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_acgrip_ignores_other_users_already_signed_in_leaderboard() -> None:
    browser = FakeAcgripBrowser(
        body=(
            "今天签到了吗？请选择您此刻的心情图片 "
            "签到排行榜 用户甲 今天已签到 用户乙 今天已签到"
        ),
        response_text="签到成功，获得 2 枚VC币",
    )

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert len(browser.submissions) == 1


@pytest.mark.asyncio
async def test_acgrip_expired_session_requires_login() -> None:
    browser = FakeAcgripBrowser(logged_in=False, body="登录 立即注册")

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_acgrip_missing_form_is_failure() -> None:
    browser = FakeAcgripBrowser(formhash=None, body="今天签到了吗？")

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "read_formhash"


@pytest.mark.asyncio
async def test_acgrip_submit_login_prompt_requires_login() -> None:
    browser = FakeAcgripBrowser(response_text="您需要先登录才能继续本操作")

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False


@pytest.mark.asyncio
async def test_acgrip_unknown_response_is_not_marked_successful() -> None:
    browser = FakeAcgripBrowser(response_text="<root>未知响应</root>")

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.details["result_excerpt"] == "未知响应"


@pytest.mark.asyncio
async def test_acgrip_missing_daily_message_is_reported_clearly() -> None:
    browser = FakeAcgripBrowser(
        response_text="您的今日想说内容忘了填，请修改后再次提交!",
    )

    result = await AcgripPlugin().sign(
        PluginContext(account_id="a1", account_label="ACGRip", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.message == "ACGRip 拒绝了空签到留言，请检查“今日最想说模式”。"
