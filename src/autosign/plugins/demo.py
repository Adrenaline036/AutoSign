from __future__ import annotations

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


class DemoPlugin(AutoSignPlugin):
    """Contract test plugin. It never contacts an external website."""

    manifest = PluginManifest(
        id="demo",
        name="Demo 签到插件",
        version="0.1.0",
        description="用于验证核心平台生命周期，不访问任何真实网站。",
        login_url="/demo-login",
        login_success_selectors=("#demo-authenticated",),
        capabilities={
            PluginCapability.HTTP_SIGN,
            PluginCapability.INTERACTIVE_LOGIN,
        },
        settings_schema={
            "type": "object",
            "properties": {
                "reward": {
                    "type": "integer",
                    "title": "模拟奖励",
                    "default": 1,
                    "minimum": 0,
                }
            },
            "additionalProperties": False,
        },
    )

    async def check_session(self, context: PluginContext) -> SessionResult:
        return SessionResult(state=SessionState.VALID, message="Demo 会话始终有效")

    async def sign(self, context: PluginContext) -> SignResult:
        reward = int(context.settings.get("reward", 1))
        context.logger.info("Demo sign executed for account %s", context.account_id)
        return SignResult(
            status=SignStatus.SUCCESS,
            message=f"Demo 签到成功，模拟获得 {reward} 点奖励",
            verified=True,
            details={"reward": reward},
        )
