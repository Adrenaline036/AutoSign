import pytest

from autosign.core.plugin_registry import PluginRegistry
from autosign.core.runner import PluginRunner
from autosign.plugin_sdk import SignStatus


@pytest.mark.asyncio
async def test_runner_adds_core_execution_metadata() -> None:
    registry = PluginRegistry()
    registry.discover()
    runner = PluginRunner(registry)

    result = await runner.execute(
        "demo",
        account_id="account-1",
        account_label="测试账户",
        settings={"reward": 7},
    )

    assert result.status is SignStatus.SUCCESS
    assert result.plugin_id == "demo"
    assert result.account_id == "account-1"
    assert result.details == {"reward": 7}
    assert result.executed_at is not None
    assert result.duration_ms is not None

