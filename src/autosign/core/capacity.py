from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

BrowserCapacityKind = Literal["automation", "interactive"]


class BrowserCapacityClosedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrowserCapacitySnapshot:
    automation_limit: int
    automation_active: int
    automation_waiting: int
    interactive_limit: int
    interactive_active: int
    interactive_waiting: int
    closing: bool


@dataclass(slots=True)
class _CapacityRequest:
    kind: BrowserCapacityKind
    future: asyncio.Future[None]
    token_id: str | None = None


@dataclass(slots=True)
class BrowserCapacityLease:
    _gate: BrowserCapacityGate
    kind: BrowserCapacityKind
    token_id: str
    _released: bool = field(default=False, init=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._gate._release(self.token_id)


class BrowserCapacityGate:
    """Cancellation-safe FIFO capacity leases for browser operations."""

    def __init__(self, *, automation_limit: int = 2, interactive_limit: int = 1) -> None:
        if automation_limit < 1 or interactive_limit < 1:
            raise ValueError("Browser capacity limits must be positive.")
        self._limits: dict[BrowserCapacityKind, int] = {
            "automation": automation_limit,
            "interactive": interactive_limit,
        }
        self._lock = asyncio.Lock()
        self._waiters: dict[BrowserCapacityKind, deque[_CapacityRequest]] = {
            "automation": deque(),
            "interactive": deque(),
        }
        self._active: dict[str, BrowserCapacityKind] = {}
        self._closing = False
        self._drained = asyncio.Event()
        self._drained.set()

    async def acquire(self, kind: BrowserCapacityKind) -> BrowserCapacityLease:
        loop = asyncio.get_running_loop()
        request = _CapacityRequest(kind=kind, future=loop.create_future())
        async with self._lock:
            if self._closing:
                raise BrowserCapacityClosedError("Browser capacity gate is closing.")
            self._waiters[kind].append(request)
            self._drained.clear()
            self._dispatch_locked(kind)
        try:
            await asyncio.shield(request.future)
        except BaseException:
            async with self._lock:
                if request.token_id is not None:
                    self._active.pop(request.token_id, None)
                else:
                    with suppress(ValueError):
                        self._waiters[kind].remove(request)
                self._dispatch_locked(kind)
                self._set_drained_locked()
            raise
        assert request.token_id is not None
        return BrowserCapacityLease(self, kind, request.token_id)

    async def begin_close(self) -> None:
        async with self._lock:
            if self._closing:
                return
            self._closing = True
            error = BrowserCapacityClosedError("Browser capacity gate is closing.")
            for waiters in self._waiters.values():
                while waiters:
                    request = waiters.popleft()
                    if not request.future.done():
                        request.future.set_exception(error)
            self._set_drained_locked()

    async def wait_drained(self) -> None:
        await self._drained.wait()

    async def snapshot(self) -> BrowserCapacitySnapshot:
        async with self._lock:
            counts = {
                kind: sum(active_kind == kind for active_kind in self._active.values())
                for kind in self._limits
            }
            return BrowserCapacitySnapshot(
                automation_limit=self._limits["automation"],
                automation_active=counts["automation"],
                automation_waiting=len(self._waiters["automation"]),
                interactive_limit=self._limits["interactive"],
                interactive_active=counts["interactive"],
                interactive_waiting=len(self._waiters["interactive"]),
                closing=self._closing,
            )

    async def _release(self, token_id: str) -> None:
        async with self._lock:
            kind = self._active.pop(token_id, None)
            if kind is None:
                return
            self._dispatch_locked(kind)
            self._set_drained_locked()

    def _dispatch_locked(self, kind: BrowserCapacityKind) -> None:
        waiters = self._waiters[kind]
        active_count = sum(active_kind == kind for active_kind in self._active.values())
        while not self._closing and waiters and active_count < self._limits[kind]:
            request = waiters.popleft()
            if request.future.done():
                continue
            token_id = str(uuid4())
            request.token_id = token_id
            self._active[token_id] = kind
            active_count += 1
            request.future.set_result(None)

    def _set_drained_locked(self) -> None:
        if not self._active and not any(self._waiters.values()):
            self._drained.set()
        else:
            self._drained.clear()
