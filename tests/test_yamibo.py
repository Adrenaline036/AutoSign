from __future__ import annotations

import pytest

from autosign.plugin_sdk import PluginContext, SignStatus
from autosign.plugins.yamibo import YamiboPlugin


class FakeYamiboBrowser:
    def __init__(
        self,
        *,
        formhash: str | None,
        message: str,
        body: str = "",
        html: str = "<html><body>Discuz</body></html>",
        formhashes: list[str | None] | None = None,
        statuses: list[int] | None = None,
    ) -> None:
        self.formhash = formhash
        self.formhashes = formhashes if formhashes is not None else [formhash]
        self.message = message
        self.body = body
        self.html = html
        self.statuses = statuses or [200]
        self.visits: list[tuple[str, str | None]] = []
        self.input_value_calls = 0

    async def goto(self, url: str, *, referrer: str | None = None) -> int:
        self.visits.append((url, referrer))
        index = min(len(self.visits) - 1, len(self.statuses) - 1)
        return self.statuses[index]

    async def input_value(self, _selector: str) -> str | None:
        index = min(self.input_value_calls, len(self.formhashes) - 1)
        self.input_value_calls += 1
        return self.formhashes[index]

    async def text_content(self, _selector: str) -> str | None:
        return self.message or None

    async def body_text(self) -> str:
        return self.body or self.message

    async def html_content(self) -> str:
        return self.html


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
async def test_yamibo_allows_initial_waf_challenge_to_finish() -> None:
    browser = FakeYamiboBrowser(
        formhash="token",
        formhashes=[None, None, "token"],
        message="签到成功！",
        statuses=[405, 200],
    )
    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert result.details["initial_http_status"] == 405
    assert result.details["formhash_attempts"] == 3
    assert browser.input_value_calls == 3
    assert browser.visits[0] == (YamiboPlugin.SIGN_URL, None)
    assert len(browser.visits) == 2


@pytest.mark.asyncio
async def test_yamibo_reports_waf_challenge_that_never_finishes() -> None:
    browser = FakeYamiboBrowser(
        formhash=None,
        formhashes=[None] * YamiboPlugin.WAF_FORMHASH_ATTEMPTS,
        message="",
        html=(
            "<html><head><script>window.__noxExpire=30;window.__noxImd=1;</script>"
            '<script src="/static/nox_20260413.js"></script>'
            '<script src="/static/gangplank_20251103.js"></script></head><body></body></html>'
        ),
        statuses=[405],
    )

    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.details["stage"] == "waf_challenge"
    assert result.details["initial_http_status"] == 405
    assert result.details["formhash_attempts"] == YamiboPlugin.WAF_FORMHASH_ATTEMPTS
    assert "nox_" in result.details["waf_markers"]
    assert "WAF" in result.message
    assert browser.input_value_calls == YamiboPlugin.WAF_FORMHASH_ATTEMPTS
    assert browser.visits == [(YamiboPlugin.SIGN_URL, None)]


@pytest.mark.asyncio
async def test_yamibo_reports_missing_formhash_after_challenge_finishes() -> None:
    browser = FakeYamiboBrowser(
        formhash=None,
        formhashes=[None] * YamiboPlugin.WAF_FORMHASH_ATTEMPTS,
        message="",
        body="百合会签到页面",
        html="<html><head></head><body>百合会签到页面</body></html>",
        statuses=[405],
    )

    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "read_formhash"
    assert result.details["initial_http_status"] == 405
    assert result.details["result_excerpt"] == "百合会签到页面"
    assert browser.input_value_calls == YamiboPlugin.WAF_FORMHASH_ATTEMPTS
    assert browser.visits == [(YamiboPlugin.SIGN_URL, None)]


@pytest.mark.asyncio
async def test_yamibo_does_not_wait_on_unknown_http_error() -> None:
    browser = FakeYamiboBrowser(
        formhash=None,
        formhashes=[None] * YamiboPlugin.WAF_FORMHASH_ATTEMPTS,
        message="",
        statuses=[503],
    )

    result = await YamiboPlugin().sign(
        PluginContext(account_id="a1", account_label="百合会", browser=browser)
    )

    assert result.status is SignStatus.FAILED
    assert result.details["stage"] == "open_sign_page"
    assert result.details["initial_http_status"] == 503
    assert result.details["formhash_attempts"] == 1
    assert browser.input_value_calls == 1
    assert browser.visits == [(YamiboPlugin.SIGN_URL, None)]


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
