from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points

from autosign.plugin_sdk import AutoSignPlugin
from autosign.plugins.acgrip import AcgripPlugin
from autosign.plugins.baidu_tieba import BaiduTiebaPlugin
from autosign.plugins.demo import DemoPlugin
from autosign.plugins.yamibo import YamiboPlugin


class PluginRegistry:
    """Discovers plugins without teaching the core about site behavior."""

    def __init__(self, builtins: Iterable[type[AutoSignPlugin]] | None = None) -> None:
        self._plugin_types = list(
            builtins or [DemoPlugin, AcgripPlugin, BaiduTiebaPlugin, YamiboPlugin]
        )
        self._plugins: dict[str, AutoSignPlugin] = {}

    def discover(self) -> None:
        plugin_types = list(self._plugin_types)
        for entry_point in entry_points(group="autosign.plugins"):
            plugin_types.append(entry_point.load())

        discovered: dict[str, AutoSignPlugin] = {}
        for plugin_type in plugin_types:
            plugin = plugin_type()
            plugin_id = plugin.manifest.id
            if plugin_id in discovered:
                raise ValueError(f"Duplicate plugin id: {plugin_id}")
            discovered[plugin_id] = plugin
        self._plugins = discovered

    def all(self) -> list[AutoSignPlugin]:
        return sorted(self._plugins.values(), key=lambda item: item.manifest.name)

    def get(self, plugin_id: str) -> AutoSignPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as exc:
            raise LookupError(f"Unknown plugin: {plugin_id}") from exc
