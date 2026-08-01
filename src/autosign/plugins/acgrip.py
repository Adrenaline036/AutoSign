from __future__ import annotations

import html
import re

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


class AcgripPlugin(AutoSignPlugin):
    """ACGRip Discuz DSU daily sign-in connector."""

    SIGN_URL = "https://bbs.acgrip.com/dsu_paulsign-sign.html"
    SUBMIT_URL = (
        "https://bbs.acgrip.com/plugin.php"
        "?id=dsu_paulsign:sign&operation=qiandao&infloat=1&inajax=1"
    )
    FORMHASH_SELECTOR = '#qiandao input[name="formhash"]'
    LOGOUT_SELECTOR = 'a[href*="action=logout"]'

    manifest = PluginManifest(
        id="acgrip",
        name="ACGRip 动漫字幕论坛",
        version="0.2.0",
        description="使用已保存的登录状态执行 ACGRip 每日签到，并验证返回结果。",
        domains=["bbs.acgrip.com"],
        login_url="https://bbs.acgrip.com/member.php?mod=logging&action=login",
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
            message="ACGRip 会话将在浏览器中检测。",
        )

    async def sign(self, context: PluginContext) -> SignResult:
        browser = context.browser
        if browser is None:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="尚未保存 ACGRip 登录状态，请先完成交互登录。",
                verified=False,
            )

        status = await browser.goto(self.SIGN_URL)
        if status is not None and status >= 400:
            return SignResult(
                status=SignStatus.FAILED,
                message=f"ACGRip 签到页面返回 HTTP {status}。",
                verified=False,
                details={"http_status": status},
            )

        logout_text = await browser.text_content(self.LOGOUT_SELECTOR)
        body = await browser.body_text()
        if logout_text is None:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="ACGRip 登录状态已失效，请重新进行交互登录。",
                verified=False,
            )

        formhash = await browser.input_value(self.FORMHASH_SELECTOR)
        if not formhash:
            already_marker = self._find_marker(
                body,
                ("今日已签到", "今天已经签到", "今天已签到", "您今天已经签到过了"),
            )
            if already_marker is not None:
                return SignResult(
                    status=SignStatus.ALREADY_SIGNED,
                    message="ACGRip 今日已经签到。",
                    verified=True,
                    details={"result_excerpt": self._compact_text(body)},
                )
            if "签到时间还未开始" in body or "签到时间已过" in body:
                return SignResult(
                    status=SignStatus.FAILED,
                    message="当前不在 ACGRip 允许的签到时间内。",
                    verified=False,
                    details={"result_excerpt": self._compact_text(body)},
                )
            return SignResult(
                status=SignStatus.FAILED,
                message="ACGRip 签到页面中未找到签到表单，网站结构可能已经变化。",
                verified=False,
                details={"stage": "read_formhash", "result_excerpt": self._compact_text(body)},
            )

        response = await browser.post_form(
            self.SUBMIT_URL,
            {
                "formhash": formhash,
                "qdxq": "kx",
                "qdmode": "3",
                "todaysay": "",
                "fastreply": "0",
            },
        )
        result_text = self._compact_text(response.text)
        details = {
            "http_status": response.status,
            "result_excerpt": result_text,
        }
        if response.status >= 400:
            return SignResult(
                status=SignStatus.FAILED,
                message=f"ACGRip 签到提交返回 HTTP {response.status}。",
                verified=False,
                details=details,
            )
        if self._find_marker(
            result_text,
            ("请先登录", "需要先登录", "您需要先登录", "登录后才能"),
        ):
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="ACGRip 登录状态已失效，请重新进行交互登录。",
                verified=False,
                details=details,
            )
        if self._find_marker(
            result_text,
            ("今日已签到", "今天已经签到", "今天已签到", "已经签到过"),
        ):
            return SignResult(
                status=SignStatus.ALREADY_SIGNED,
                message="ACGRip 今日已经签到。",
                verified=True,
                details=details,
            )
        if "今日想说内容忘了填" in result_text:
            return SignResult(
                status=SignStatus.FAILED,
                message="ACGRip 拒绝了空签到留言，请检查“今日最想说模式”。",
                verified=False,
                details=details,
            )
        if self._find_marker(result_text, ("签到成功", "恭喜你签到成功", "签到完毕")):
            return SignResult(
                status=SignStatus.SUCCESS,
                message=result_text[:300] or "ACGRip 签到成功。",
                verified=True,
                details=details,
            )
        return SignResult(
            status=SignStatus.FAILED,
            message="ACGRip 没有返回可确认的签到结果，已保留响应摘要供检查。",
            verified=False,
            details=details,
        )

    @staticmethod
    def _find_marker(text: str, markers: tuple[str, ...]) -> str | None:
        return next((marker for marker in markers if marker in text), None)

    @staticmethod
    def _compact_text(text: str, *, limit: int = 500) -> str:
        decoded = html.unescape(text)
        decoded = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", decoded, flags=re.DOTALL)
        without_tags = re.sub(r"<[^>]+>", " ", decoded)
        return " ".join(without_tags.split())[:limit]
