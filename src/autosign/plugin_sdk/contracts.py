from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class PluginCapability(StrEnum):
    INTERACTIVE_LOGIN = "interactive_login"
    HTTP_SIGN = "http_sign"
    BROWSER_SIGN = "browser_sign"
    BROWSER_FALLBACK = "browser_fallback"


class SessionState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    INTERACTION_REQUIRED = "interaction_required"
    UNKNOWN = "unknown"


class SignStatus(StrEnum):
    SUCCESS = "success"
    ALREADY_SIGNED = "already_signed"
    FAILED = "failed"
    INTERACTION_REQUIRED = "interaction_required"


class PluginManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    version: str
    api_version: int = 1
    description: str = ""
    domains: list[str] = Field(default_factory=list)
    login_url: str | None = None
    login_success_selectors: tuple[str, ...] = ()
    login_cookie_name_suffixes: tuple[str, ...] = ()
    capabilities: set[PluginCapability] = Field(default_factory=set)
    settings_schema: dict[str, Any] = Field(default_factory=dict)


class SessionResult(BaseModel):
    state: SessionState
    message: str = ""


class SignResult(BaseModel):
    status: SignStatus
    message: str
    verified: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    plugin_id: str | None = None
    account_id: str | None = None
    executed_at: datetime | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class BrowserResponse:
    """Small, serializable response returned by browser-origin HTTP requests."""

    status: int
    url: str
    text: str


class SecretAccessor(Protocol):
    """Account-scoped secret access exposed to one plugin execution."""

    def get(self, name: str) -> str:
        ...

    def names(self) -> list[str]:
        ...


class BrowserAutomation(Protocol):
    """Small browser surface supplied by core to browser-based sign-in plugins."""

    async def goto(self, url: str, *, referrer: str | None = None) -> int | None:
        ...

    async def input_value(self, selector: str) -> str | None:
        ...

    async def text_content(self, selector: str) -> str | None:
        ...

    async def body_text(self) -> str:
        ...

    async def html_content(self) -> str:
        ...

    async def click(self, selector: str) -> bool:
        """Click one plugin-owned selector and report whether it was actionable."""
        ...

    async def post_form(
        self,
        url: str,
        data: Mapping[str, str],
    ) -> BrowserResponse:
        ...

    async def post_json(
        self,
        url: str,
        data: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> BrowserResponse:
        """POST JSON through the browser context without rendering a site page."""
        ...

    async def storage_value(self, origin: str, key: str) -> Any | None:
        """Read one restored IndexedDB record by its out-of-line key."""
        ...

    async def write_storage_value(self, key: str, value: Any) -> bool:
        """Replace one IndexedDB record on the current page origin."""
        ...


class EmptySecretAccessor:
    def get(self, name: str) -> str:
        raise LookupError(f"No account secret is available: {name}")

    def names(self) -> list[str]:
        return []


@dataclass(slots=True)
class PluginContext:
    """Capabilities supplied by core; concrete services will be added incrementally."""

    account_id: str
    account_label: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("autosign.plugin"))
    http: Any | None = None
    browser: BrowserAutomation | None = None
    secrets: SecretAccessor = field(default_factory=EmptySecretAccessor, repr=False)


class AutoSignPlugin(ABC):
    manifest: PluginManifest

    @abstractmethod
    async def check_session(self, context: PluginContext) -> SessionResult:
        """Return whether the stored site session can be used."""

    @abstractmethod
    async def sign(self, context: PluginContext) -> SignResult:
        """Execute and verify one account-level sign-in task."""
