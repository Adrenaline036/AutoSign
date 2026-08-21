import pytest

from autosign.core.plugin_registry import PluginRegistry
from autosign.plugin_sdk import (
    PLUGIN_API_VERSION,
    AutoSignPlugin,
    PluginContext,
    PluginManifest,
    SignResult,
)


class FuturePlugin(AutoSignPlugin):
    manifest = PluginManifest(
        id="future",
        name="Future",
        version="1.0.0",
        api_version=2,
    )

    async def sign(self, context: PluginContext) -> SignResult:
        raise AssertionError("The unsupported plugin must never execute.")


def test_registry_discovers_builtin_plugins() -> None:
    registry = PluginRegistry()
    registry.discover()

    plugins = registry.all()

    assert [plugin.manifest.id for plugin in plugins] == [
        "acgrip",
        "demo",
        "vikacg",
        "yamibo",
        "baidu_tieba",
    ]
    assert PluginManifest(id="default", name="Default", version="1.0.0").api_version == (
        PLUGIN_API_VERSION
    )
    assert registry.get("demo").manifest.api_version == PLUGIN_API_VERSION


def test_registry_rejects_unsupported_plugin_api_version() -> None:
    registry = PluginRegistry(builtins=[FuturePlugin])

    with pytest.raises(ValueError, match="Unsupported plugin API version for future: 2"):
        registry.discover()
