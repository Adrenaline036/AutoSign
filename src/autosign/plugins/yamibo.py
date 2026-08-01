from __future__ import annotations

from urllib.parse import quote

from autosign.plugin_sdk import (
    AutoSignPlugin,
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
        version="0.2.0",
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

        status = await browser.goto(self.SIGN_URL)
        if status is not None and status >= 400:
            return SignResult(
                status=SignStatus.FAILED,
                message=f"百合会签到页面返回 HTTP {status}。",
                verified=False,
                details={"http_status": status},
            )

        formhash = await browser.input_value(self.FORMHASH_SELECTOR)
        if not formhash:
            body = await browser.body_text()
            if "登录" in body or "成为会员" in body:
                return SignResult(
                    status=SignStatus.INTERACTION_REQUIRED,
                    message="百合会登录状态已失效，请重新进行交互登录。",
                    verified=False,
                )
            return SignResult(
                status=SignStatus.FAILED,
                message="百合会签到页面中未找到 formhash，网站结构可能已经变化。",
                verified=False,
                details={"stage": "read_formhash"},
            )

        sign_url = f"{self.SIGN_URL}&sign={quote(formhash, safe='')}"
        sign_status = await browser.goto(sign_url, referrer=self.SIGN_URL)
        message = self._repair_utf8_mojibake(
            await browser.text_content(self.MESSAGE_SELECTOR)
            or await browser.body_text()
        )
        details = {
            "http_status": sign_status,
            "result_excerpt": message[:300],
        }
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
