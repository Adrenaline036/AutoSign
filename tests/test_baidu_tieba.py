from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from autosign.plugin_sdk import BrowserResponse, PluginContext, SignStatus
from autosign.plugins.baidu_tieba import BaiduTiebaPlugin


class FakeTiebaBrowser:
    def __init__(
        self,
        *,
        logged_in: bool = True,
        pages: dict[int, list[str]] | None = None,
        responses: dict[str, tuple[int, dict[str, object] | str]] | None = None,
        tbs_status: int = 200,
        recognized_page: bool = True,
    ) -> None:
        self.logged_in = logged_in
        self.pages = pages if pages is not None else {1: ["测试吧"]}
        self.responses = responses or {}
        self.tbs_status = tbs_status
        self.recognized_page = recognized_page
        self.current_url = ""
        self.visits: list[str] = []
        self.submissions: list[tuple[str, dict[str, str]]] = []

    async def goto(self, url: str, *, referrer: str | None = None) -> int:
        self.current_url = url
        self.visits.append(url)
        return self.tbs_status if url == BaiduTiebaPlugin.TBS_URL else 200

    async def body_text(self) -> str:
        if self.current_url == BaiduTiebaPlugin.TBS_URL:
            return json.dumps({"tbs": "test-tbs", "is_login": int(self.logged_in)})
        return ""

    async def html_content(self) -> str:
        page_number = int(self.current_url.rsplit("=", 1)[-1])
        marker_start = '<div class="forum_table">管理我喜欢的吧<table><tr></tr>'
        marker_end = "</table></div>"
        links = "".join(
            '<tr><td><a href="/f?kw='
            f'{quote(name, encoding="gb18030")}&fr=home">{name}吧</a></td></tr>'
            for name in self.pages.get(page_number, [])
        )
        table = f"{marker_start}{links}{marker_end}" if self.recognized_page else links
        return f'<html><body><a href="/f?kw=推荐吧">推荐</a>{table}</body></html>'

    async def input_value(self, _selector: str) -> str | None:
        return None

    async def text_content(self, _selector: str) -> str | None:
        return None

    async def post_form(self, url: str, data: dict[str, str]) -> BrowserResponse:
        self.submissions.append((url, data))
        status, payload = self.responses.get(
            data["kw"],
            (200, {"no": 0, "error": "", "data": {}}),
        )
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return BrowserResponse(status=status, url=url, text=text)


def context(browser: FakeTiebaBrowser | None) -> PluginContext:
    return PluginContext(account_id="a1", account_label="我的贴吧", browser=browser)


@pytest.mark.asyncio
async def test_tieba_requires_saved_login() -> None:
    result = await BaiduTiebaPlugin().sign(context(None))

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False


@pytest.mark.asyncio
async def test_tieba_expired_session_requires_login() -> None:
    browser = FakeTiebaBrowser(logged_in=False)

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert browser.submissions == []


@pytest.mark.asyncio
async def test_tieba_signs_all_followed_forums_across_pages() -> None:
    browser = FakeTiebaBrowser(pages={1: ["甲", "乙"], 2: ["丙"], 3: []})

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.SUCCESS
    assert result.verified is True
    assert result.details["total"] == 3
    assert result.details["success"] == 3
    assert [submission[1]["kw"] for submission in browser.submissions] == ["甲", "乙", "丙"]
    assert all(submission[1]["tbs"] == "test-tbs" for submission in browser.submissions)


@pytest.mark.asyncio
async def test_tieba_decodes_real_gbk_encoded_forum_keyword() -> None:
    browser = FakeTiebaBrowser(pages={1: ["赛马娘"], 2: []})

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.SUCCESS
    assert browser.submissions[0][1]["kw"] == "赛马娘"


@pytest.mark.asyncio
async def test_tieba_all_already_signed_is_verified() -> None:
    browser = FakeTiebaBrowser(
        pages={1: ["甲", "乙"], 2: []},
        responses={
            "甲": (200, {"no": 1101, "error": "亲，你之前已经签过了"}),
            "乙": (200, {"no": 1101, "error": "今日已签到"}),
        },
    )

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert result.details["already_signed"] == 2


@pytest.mark.asyncio
async def test_tieba_partial_failure_is_not_verified_and_names_failures() -> None:
    browser = FakeTiebaBrowser(
        pages={1: ["甲", "乙"], 2: []},
        responses={"乙": (200, {"no": 999, "error": "操作过于频繁"})},
    )

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.details["success"] == 1
    assert result.details["failed"] == 1
    assert "乙" in result.message


@pytest.mark.asyncio
async def test_tieba_session_expiry_during_batch_requires_login() -> None:
    browser = FakeTiebaBrowser(
        pages={1: ["甲"], 2: []},
        responses={"甲": (200, {"no": 1102, "error": "用户未登录"})},
    )

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.INTERACTION_REQUIRED
    assert result.verified is False
    assert result.details["interaction_required"] == 1


@pytest.mark.asyncio
async def test_tieba_ignores_forum_links_outside_followed_table() -> None:
    browser = FakeTiebaBrowser(pages={1: ["甲"], 2: []})

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.SUCCESS
    assert [submission[1]["kw"] for submission in browser.submissions] == ["甲"]


@pytest.mark.asyncio
async def test_tieba_empty_recognized_follow_list_is_noop() -> None:
    browser = FakeTiebaBrowser(pages={1: []})

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.ALREADY_SIGNED
    assert result.verified is True
    assert result.details["total"] == 0


@pytest.mark.asyncio
async def test_tieba_unknown_follow_page_fails_safely() -> None:
    browser = FakeTiebaBrowser(pages={1: []}, recognized_page=False)

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.FAILED
    assert result.verified is False
    assert result.details["stage"] == "parse_followed_forums"


@pytest.mark.asyncio
async def test_tieba_tbs_http_failure_does_not_submit() -> None:
    browser = FakeTiebaBrowser(tbs_status=503)

    result = await BaiduTiebaPlugin().sign(context(browser))

    assert result.status is SignStatus.FAILED
    assert result.details["http_status"] == 503
    assert browser.submissions == []
