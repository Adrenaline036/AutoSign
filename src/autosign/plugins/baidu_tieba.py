from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlparse

from autosign.plugin_sdk import (
    AutoSignPlugin,
    PluginCapability,
    PluginContext,
    PluginManifest,
    SignResult,
    SignStatus,
)


class _FollowedForumParser(HTMLParser):
    """Extract followed forum names from Baidu's account management table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forums: list[str] = []
        self.table_rows = 0
        self._table_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if self._table_depth:
            self._table_depth += 1
        elif "forum_table" in classes:
            self._table_depth = 1
        if not self._table_depth:
            return
        if tag.lower() == "tr":
            self.table_rows += 1
        if tag.lower() != "a":
            return
        href = attributes.get("href")
        if not href:
            return
        parsed = urlparse(href)
        if parsed.path.rstrip("/") != "/f":
            return
        keyword = parse_qs(
            parsed.query,
            encoding="gb18030",
            errors="replace",
        ).get("kw", [""])[0].strip()
        if "\ufffd" in keyword:
            return
        if keyword and keyword not in self.forums:
            self.forums.append(keyword)

    def handle_endtag(self, tag: str) -> None:
        if self._table_depth:
            self._table_depth -= 1


class BaiduTiebaPlugin(AutoSignPlugin):
    """Baidu Tieba followed-forum batch sign-in connector."""

    LOGIN_URL = (
        "https://passport.baidu.com/v2/?login&tpl=tb&u="
        "https%3A%2F%2Ftieba.baidu.com%2Ff%2Flike%2Fmylike"
    )
    TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
    FOLLOWED_URL = "https://tieba.baidu.com/f/like/mylike?pn={page}"
    SIGN_URL = "https://tieba.baidu.com/sign/add"
    MAX_PAGES = 100
    MAX_FORUMS = 500

    manifest = PluginManifest(
        id="baidu_tieba",
        name="百度贴吧",
        version="0.1.1",
        description="获取当前百度账户关注的贴吧并逐吧签到，汇总每个贴吧的结果。",
        domains=["tieba.baidu.com", "passport.baidu.com"],
        login_url=LOGIN_URL,
        login_success_selectors=(
            'a[href*="passport.baidu.com/?logout"]',
            'a[href*="un="]',
            ".u_username",
        ),
        login_cookie_name_suffixes=("BDUSS", "STOKEN"),
        capabilities={
            PluginCapability.INTERACTIVE_LOGIN,
            PluginCapability.BROWSER_SIGN,
        },
    )

    async def sign(self, context: PluginContext) -> SignResult:
        browser = context.browser
        if browser is None:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="尚未保存百度贴吧登录状态，请先完成交互登录。",
                verified=False,
            )

        status = await browser.goto(self.TBS_URL)
        if status is not None and status >= 400:
            return SignResult(
                status=SignStatus.FAILED,
                message=f"百度贴吧登录态接口返回 HTTP {status}。",
                verified=False,
                details={"stage": "read_tbs", "http_status": status},
            )
        session_data = self._parse_json(await browser.body_text())
        if session_data is None:
            return SignResult(
                status=SignStatus.FAILED,
                message="百度贴吧登录态接口返回了无法识别的内容。",
                verified=False,
                details={"stage": "read_tbs"},
            )
        if not self._is_true(session_data.get("is_login")):
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message="百度贴吧登录状态已失效，请重新进行交互登录。",
                verified=False,
            )
        tbs = str(session_data.get("tbs", "")).strip()
        if not tbs:
            return SignResult(
                status=SignStatus.FAILED,
                message="百度贴吧没有返回签到所需的 tbs，暂未提交任何签到。",
                verified=False,
                details={"stage": "read_tbs"},
            )

        forums, list_error = await self._load_followed_forums(browser)
        if list_error is not None:
            return list_error
        if not forums:
            return SignResult(
                status=SignStatus.ALREADY_SIGNED,
                message="当前百度账户没有关注的贴吧，无需签到。",
                verified=True,
                details={"total": 0, "success": 0, "already_signed": 0, "failed": 0},
            )

        results: list[dict[str, Any]] = []
        for forum in forums:
            response = await browser.post_form(
                self.SIGN_URL,
                {"ie": "utf-8", "kw": forum, "tbs": tbs},
            )
            results.append(self._classify_sign_response(forum, response.status, response.text))

        success_count = sum(item["status"] == "success" for item in results)
        already_count = sum(item["status"] == "already_signed" for item in results)
        failed_items = [item for item in results if item["status"] == "failed"]
        login_items = [item for item in results if item["status"] == "interaction_required"]
        details = {
            "total": len(results),
            "success": success_count,
            "already_signed": already_count,
            "failed": len(failed_items),
            "interaction_required": len(login_items),
            "forums": results,
        }
        summary = (
            f"百度贴吧共处理 {len(results)} 个关注吧：签到成功 {success_count} 个，"
            f"今日已签 {already_count} 个，失败 {len(failed_items)} 个。"
        )
        if login_items:
            return SignResult(
                status=SignStatus.INTERACTION_REQUIRED,
                message=f"{summary} 签到期间登录状态失效，请重新进行交互登录。",
                verified=False,
                details=details,
            )
        if failed_items:
            failed_names = "、".join(item["forum"] for item in failed_items[:5])
            if len(failed_items) > 5:
                failed_names += "等"
            return SignResult(
                status=SignStatus.FAILED,
                message=f"{summary} 失败贴吧：{failed_names}",
                verified=False,
                details=details,
            )
        if success_count:
            return SignResult(
                status=SignStatus.SUCCESS,
                message=summary,
                verified=True,
                details=details,
            )
        return SignResult(
            status=SignStatus.ALREADY_SIGNED,
            message=summary,
            verified=True,
            details=details,
        )

    async def _load_followed_forums(self, browser: Any) -> tuple[list[str], SignResult | None]:
        forums: list[str] = []
        recognized_page = False
        unparsed_data_rows = 0
        for page_number in range(1, self.MAX_PAGES + 1):
            status = await browser.goto(self.FOLLOWED_URL.format(page=page_number))
            if status is not None and status >= 400:
                return [], SignResult(
                    status=SignStatus.FAILED,
                    message=f"百度贴吧关注列表返回 HTTP {status}。",
                    verified=False,
                    details={
                        "stage": "list_followed_forums",
                        "page": page_number,
                        "http_status": status,
                    },
                )
            page_html = await browser.html_content()
            recognized_page = recognized_page or (
                "forum_table" in page_html or "管理我喜欢的吧" in page_html
            )
            parser = _FollowedForumParser()
            parser.feed(page_html)
            unparsed_data_rows += max(0, parser.table_rows - 1 - len(parser.forums))
            new_forums = [name for name in parser.forums if name not in forums]
            if not new_forums:
                break
            forums.extend(new_forums)
            if len(forums) >= self.MAX_FORUMS:
                forums = forums[: self.MAX_FORUMS]
                break

        if not recognized_page or unparsed_data_rows:
            return [], SignResult(
                status=SignStatus.FAILED,
                message="未能识别百度贴吧关注列表，页面结构可能已经变化。",
                verified=False,
                details={"stage": "parse_followed_forums"},
            )
        return forums, None

    @classmethod
    def _classify_sign_response(
        cls,
        forum: str,
        http_status: int,
        response_text: str,
    ) -> dict[str, Any]:
        payload = cls._parse_json(response_text)
        error_text = str(payload.get("error", "") if payload else "").strip()
        result_code = payload.get("no") if payload else None
        excerpt = " ".join((error_text or response_text).split())[:200]
        if http_status < 400 and str(result_code) == "0":
            status = "success"
        elif any(marker in error_text for marker in ("已经签", "已签到", "签过")):
            status = "already_signed"
        elif any(marker in error_text for marker in ("登录", "BDUSS", "用户未登录")):
            status = "interaction_required"
        else:
            status = "failed"
        return {
            "forum": forum,
            "status": status,
            "code": result_code,
            "http_status": http_status,
            "message": excerpt,
        }

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_true(value: Any) -> bool:
        return value is True or str(value).strip() == "1"
