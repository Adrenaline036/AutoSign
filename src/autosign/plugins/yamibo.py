from __future__ import annotations

import asyncio
from urllib.parse import quote

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


class YamiboPlugin(AutoSignPlugin):
    """Yamibo browser sign-in connector."""

    SIGN_URL = "https://bbs.yamibo.com/plugin.php?id=zqlj_sign"
    FORMHASH_SELECTOR = '#scbar_form > input[name="formhash"]'
    MESSAGE_SELECTOR = "#messagetext > p:first-of-type"
    WAF_STATUS = 405
    WAF_FORMHASH_ATTEMPTS = 10
    WAF_MARKERS = ("nox_", "gangplank_", "__noxexpire", "__noximd")
    BODY_READ_ATTEMPTS = 3
    BODY_READ_RETRY_SECONDS = 0.5

    @staticmethod
    def _repair_utf8_mojibake(text: str) -> str:
        """Repair the site's UTF-8 text when Chromium receives it as Latin-1."""
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text
        original_cjk = sum("\u4e00" <= character <= "\u9fff" for character in text)
        repaired_cjk = sum("\u4e00" <= character <= "\u9fff" for character in repaired)
        return repaired if repaired_cjk > original_cjk else text

    manifest = PluginManifest(
        id="yamibo",
        name="百合会论坛",
        version="0.2.3",
        description="使用已保存的浏览器登录状态执行百合会每日打卡，并验证结果。",
        domains=["bbs.yamibo.com"],
        login_url="https://bbs.yamibo.com/",
        login_success_selectors=(
            "#um",
            ".vwmy",
            'a[href*="action=logout"]',
        ),
        login_cookie_name_suffixes=("_auth",),
        capabilities={
            PluginCapability.INTERACTIVE_LOGIN,
            PluginCapability.BROWSER_SIGN,
        },
    )

    async def check_session(self, context: PluginContext) -> SessionResult:
        return SessionResult(
            state=SessionState.UNKNOWN,
            message="百合会会话将在浏览器中检测。",
        )

    async def sign(self, context: PluginContext) -> SignResult:
        browser = context.browser
        if browser is None:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="尚未保存百合会登录状态，请先完成交互登录。",
                verified=False,
            )

        initial_status = await browser.goto(self.SIGN_URL)
        # Baidu WAF returns an initial HTTP 405 page and then reloads the same
        # document after its JavaScript challenge succeeds. Do not navigate
        # again: repeated input_value calls each wait up to three seconds for
        # the real Discuz page to expose its formhash.
        attempt_limit = (
            self.WAF_FORMHASH_ATTEMPTS if initial_status == self.WAF_STATUS else 1
        )
        formhash = None
        formhash_attempts = 0
        for _ in range(attempt_limit):
            formhash_attempts += 1
            formhash = await browser.input_value(self.FORMHASH_SELECTOR)
            if formhash:
                break

        base_details = {
            "initial_http_status": initial_status,
            "formhash_attempts": formhash_attempts,
        }
        if (
            initial_status is not None
            and initial_status >= 400
            and initial_status != self.WAF_STATUS
            and not formhash
        ):
            return SignResult(
                status=SignStatus.FAILED,
                message=f"百合会签到页面返回 HTTP {initial_status}。",
                verified=False,
                details={**base_details, "stage": "open_sign_page"},
            )

        if not formhash:
            html = await browser.html_content()
            html_lower = html.lower()
            waf_markers = [marker for marker in self.WAF_MARKERS if marker in html_lower]
            if initial_status == self.WAF_STATUS and waf_markers:
                return SignResult(
                    status=SignStatus.FAILED,
                    message="百合会百度 WAF 安全验证未能在限定时间内完成，请稍后重试。",
                    verified=False,
                    details={
                        **base_details,
                        "stage": "waf_challenge",
                        "waf_markers": waf_markers,
                    },
                )
            body, body_read_timeouts = await self._read_body_with_retries(browser)
            if body is None:
                return SignResult(
                    status=SignStatus.FAILED,
                    message="百合会签到页面持续切换，暂时无法读取页面内容，请稍后重试。",
                    verified=False,
                    details={
                        **base_details,
                        "stage": "read_page_body",
                        "body_read_timeouts": body_read_timeouts,
                    },
                )
            body = self._repair_utf8_mojibake(body)
            if "登录" in body or "成为会员" in body:
                return SignResult(
                    status=SignStatus.INTERACTION_REQUIRED,
                    message="百合会登录状态已失效，请重新进行交互登录。",
                    verified=False,
                    details={
                        **base_details,
                        "stage": "check_session",
                        "result_excerpt": body[:300],
                        **(
                            {"body_read_timeouts": body_read_timeouts}
                            if body_read_timeouts
                            else {}
                        ),
                    },
                )
            details = {
                **base_details,
                "stage": "read_formhash",
                "result_excerpt": body[:300],
            }
            if body_read_timeouts:
                details["body_read_timeouts"] = body_read_timeouts
            return SignResult(
                status=SignStatus.FAILED,
                message="百合会签到页面中未找到 formhash，网站结构可能已经变化。",
                verified=False,
                details=details,
            )

        sign_url = f"{self.SIGN_URL}&sign={quote(formhash, safe='')}"
        sign_status = await browser.goto(sign_url, referrer=self.SIGN_URL)
        message = await browser.text_content(self.MESSAGE_SELECTOR)
        body_read_timeouts = 0
        if not message:
            message, body_read_timeouts = await self._read_body_with_retries(browser)
        if message is None:
            return SignResult(
                status=SignStatus.FAILED,
                message="百合会提交签到后页面持续切换，暂时无法读取结果，请稍后重试。",
                verified=False,
                details={
                    "http_status": sign_status,
                    **base_details,
                    "stage": "read_sign_result",
                    "body_read_timeouts": body_read_timeouts,
                },
            )
        message = self._repair_utf8_mojibake(message)
        details = {
            "http_status": sign_status,
            **base_details,
            "result_excerpt": message[:300],
        }
        if body_read_timeouts:
            details["body_read_timeouts"] = body_read_timeouts
        if any(marker in message for marker in ("请登录", "请先登录", "需要登录", "成为会员")):
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="百合会登录状态已失效，请重新进行交互登录。",
                verified=False,
                details=details,
            )
        if "今日已打卡" in message or "打过卡" in message or "已经签到" in message:
            return SignResult(
                status=SignStatus.ALREADY_SIGNED,
                message="百合会今日已经打卡。",
                verified=True,
                details=details,
            )
        if "成功" in message:
            return SignResult(
                status=SignStatus.SUCCESS,
                message=message[:300],
                verified=True,
                details=details,
            )
        if "权限" in message:
            return SignResult(
                status=SignStatus.FAILED,
                message="当前百合会用户组没有签到权限，请检查邮箱验证和用户组等级。",
                verified=False,
                details=details,
            )
        return SignResult(
            status=SignStatus.FAILED,
            message="百合会没有返回可确认的签到结果，已保留页面摘要供检查。",
            verified=False,
            details=details,
        )

    @classmethod
    async def _read_body_with_retries(
        cls,
        browser: BrowserAutomation,
    ) -> tuple[str | None, int]:
        """Retry only transient Playwright timeouts while WAF replaces ``body``."""
        body_read_timeouts = 0
        for attempt in range(cls.BODY_READ_ATTEMPTS):
            try:
                return await browser.body_text(), body_read_timeouts
            except Exception as exc:
                message = str(exc)
                is_body_timeout = (
                    type(exc).__name__ == "TimeoutError"
                    and 'locator("body")' in message
                )
                if not is_body_timeout:
                    raise
                body_read_timeouts += 1
                if attempt + 1 < cls.BODY_READ_ATTEMPTS:
                    await asyncio.sleep(cls.BODY_READ_RETRY_SECONDS)
        return None, body_read_timeouts
