from __future__ import annotations

import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from autosign.core.plugin_registry import PluginRegistry
from autosign.plugin_sdk import BrowserAutomation, PluginContext, SecretAccessor, SignResult


class PluginRunner:
    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry
        self._logger = logging.getLogger("autosign.runner")

    async def execute(
        self,
        plugin_id: str,
        *,
        account_id: str,
        account_label: str,
        settings: dict[str, Any] | None = None,
        secrets: SecretAccessor | None = None,
        browser: BrowserAutomation | None = None,
    ) -> SignResult:
        plugin = self._registry.get(plugin_id)
        context = PluginContext(
            account_id=account_id,
            account_label=account_label,
            settings=settings or {},
            logger=self._logger,
            browser=browser,
            **({"secrets": secrets} if secrets is not None else {}),
        )
        started = perf_counter()
        result = await plugin.sign(context)
        elapsed_ms = round((perf_counter() - started) * 1000)
        return result.model_copy(
            update={
                "plugin_id": plugin_id,
                "account_id": account_id,
                "executed_at": datetime.now(UTC),
                "duration_ms": elapsed_ms,
            }
        )
