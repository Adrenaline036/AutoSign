from autosign.core.plugin_registry import PluginRegistry


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
    assert registry.get("demo").manifest.api_version == 1
