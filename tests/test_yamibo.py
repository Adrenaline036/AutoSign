from __future__ import annotations

import pytest

from autosign.plugin_sdk import PluginContext, SignStatus
from autosign.plugins.yamibo import YamiboPlugin


class FakeYamiboBrowser:
    def __init__(self, *, formhash: str | None, message: str, body: str = "") -> None:
        self.formhash = formhash
        self.message = message
        self.body = body
        self.visits: list[tuple[str, str | None]] = []

    async def goto(self, url: str, *, referrer: str | None = None) -> int:
        self.visits.append((url, referrer))
        return 200

    async def input_value(self, _selector: str) -> str | None:
        return self.formhash

    async def text_content(self, _selector: str) -> str | None:
        return self.message or None

    async def body_text(self) -> str:
        return self.body or self.message


@pytest.mark.asyncio
async def test_yamibo_sign_success() -> None:
    browser = FakeYamiboBrowser(formhash="token+value", message="签到成功！获得 2 对象")
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert browser.visits[1] == (
        "https://bbs.yamibo.com/plugin.php?id=zqlj_sign&sign=token%2Bvalue",
        YamiboPlugin.SIGN_URL,
    )


@pytest.mark.asyncio
async def test_yamibo_already_signed_is_verified() -> None:
    browser = FakeYamiboBrowser(formhash="token", message="您今天已经打过卡")
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True


@pytest.mark.asyncio
async def test_yamibo_expired_session_requires_login() -> None:
    browser = FakeYamiboBrowser(
        formhash=None,
        message="",
        body="您需要登录后才可以继续 登录 | 成为会员",
    )
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False


@pytest.mark.asyncio
async def test_yamibo_login_prompt_after_submit_requires_login() -> None:
    browser = FakeYamiboBrowser(formhash="guest-token", message="您需要登录后才可以继续")
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False


@pytest.mark.asyncio
async def test_yamibo_repairs_mojibake_login_prompt() -> None:
    mojibake = "请登录之后继续...".encode().decode("latin-1")
    browser = FakeYamiboBrowser(formhash="guest-token", message=mojibake)
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.details["result_excerpt"] == "请登录之后继续..."
