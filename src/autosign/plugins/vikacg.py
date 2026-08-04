from __future__ import annotations

import asyncio

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


class VikacgPlugin(AutoSignPlugin):
    """VikACG browser-based daily mission connector."""

    SIGN_URL = "https://www.vikacg.com/wallet/mission"
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

    manifest = PluginManifest(
        id="vikacg",
        name="VikACG 维咔",
        version="0.1.0",
        description="使用已保存的浏览器登录状态执行 VikACG 每日签到，并验证页面状态。",
        domains=["www.vikacg.com", "vikacg.com"],
        login_url="https://www.vikacg.com/sign",
        login_success_selectors=(
            'a[href="/message"]',
            'a[href="/account"]',
            'main button:has-text("立即签到")',
        ),
        capabilities={
            PluginCapability.INTERACTIVE_LOGIN,
            PluginCapability.BROWSER_SIGN,
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
        for attempt in range(self.READY_POLL_ATTEMPTS):
            body = await browser.body_text()
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
            if self._login_required(body):
                return self._interaction_required(body)
            return SignResult(
                status=SignStatus.FAILED,
                message=(
                    "VikACG 页面等待超时，未找到可确认的签到状态，"
                    "网站可能仍在加载或结构已经变化。"
                ),
                verified=False,
                details={"stage": "find_sign_button", "result_excerpt": self._excerpt(body)},
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
        for _attempt in range(self.POLL_ATTEMPTS):
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
            latest_body = await browser.body_text()
            if self._sign_succeeded(latest_body):
                return SignResult(
                    status=SignStatus.SUCCESS,
                    message="VikACG 签到成功。",
                    verified=True,
                    details={"result_excerpt": self._excerpt(latest_body)},
                )
            if self._login_required(latest_body):
                return self._interaction_required(latest_body)

        return SignResult(
            status=SignStatus.FAILED,
            message="VikACG 点击签到后没有出现可确认的成功状态。",
            verified=False,
            details={"stage": "verify_sign_result", "result_excerpt": self._excerpt(latest_body)},
        )

    @staticmethod
    def _login_required(text: str) -> bool:
        return any(
            marker in text
            for marker in ("请先登录", "登录后即可查看您的积分", "使用维咔账号登录")
        )

    @staticmethod
    def _already_signed(text: str) -> bool:
        return any(marker in text for marker in ("今日已签", "今天已签到", "今日已经签到"))

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
