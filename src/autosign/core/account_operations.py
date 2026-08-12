from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

OperationKind = Literal["use", "delete"]


class AccountOperationRejectedError(RuntimeError):
    pass


@dataclass(slots=True)
class _OperationRequest:
    kind: OperationKind
    future: asyncio.Future[None]
    token_id: str = field(default_factory=lambda: str(uuid4()))
    granted: bool = False


@dataclass(slots=True)
class _AccountEntry:
    queue: deque[_OperationRequest] = field(default_factory=deque)
    active: _OperationRequest | None = None
    deleting: bool = False


class AccountOperationGate:
    """Serialize account use/delete operations without retaining idle locks."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, _AccountEntry] = {}

    @asynccontextmanager
    async def use(self, account_id: str) -> AsyncIterator[None]:
        request = await self._acquire(account_id, "use")
        try:
            yield
        finally:
            await self._release(account_id, request, delete_succeeded=False)

    @asynccontextmanager
    async def delete(self, account_id: str) -> AsyncIterator[None]:
        request = await self._acquire(account_id, "delete")
        succeeded = False
        try:
            yield
            succeeded = True
        finally:
            await self._release(account_id, request, delete_succeeded=succeeded)

    async def entry_count(self) -> int:
        async with self._lock:
            return len(self._entries)

    async def _acquire(
        self,
        account_id: str,
        kind: OperationKind,
    ) -> _OperationRequest:
        loop = asyncio.get_running_loop()
        request = _OperationRequest(kind=kind, future=loop.create_future())
        async with self._lock:
            entry = self._entries.setdefault(account_id, _AccountEntry())
            if entry.deleting:
                raise AccountOperationRejectedError(
                    "Account deletion is in progress; try again after it finishes."
                )
            if kind == "delete":
                entry.deleting = True
            entry.queue.append(request)
            self._dispatch_locked(entry)
        try:
            await asyncio.shield(request.future)
        except BaseException:
            async with self._lock:
                entry = self._entries.get(account_id)
                if entry is not None:
                    if request.granted and entry.active is request:
                        entry.active = None
                    else:
                        with suppress(ValueError):
                            entry.queue.remove(request)
                    if request.kind == "delete":
                        entry.deleting = False
                    self._dispatch_locked(entry)
                    self._cleanup_locked(account_id, entry)
            raise
        return request

    async def _release(
        self,
        account_id: str,
        request: _OperationRequest,
        *,
        delete_succeeded: bool,
    ) -> None:
        async with self._lock:
            entry = self._entries.get(account_id)
            if entry is None or entry.active is not request:
                return
            entry.active = None
            if request.kind == "delete":
                entry.deleting = False
                if delete_succeeded:
                    entry.queue.clear()
            self._dispatch_locked(entry)
            self._cleanup_locked(account_id, entry)

    @staticmethod
    def _dispatch_locked(entry: _AccountEntry) -> None:
        if entry.active is not None:
            return
        while entry.queue:
            request = entry.queue.popleft()
            if request.future.done():
                continue
            entry.active = request
            request.granted = True
            request.future.set_result(None)
            return

    def _cleanup_locked(self, account_id: str, entry: _AccountEntry) -> None:
        if entry.active is None and not entry.queue and not entry.deleting:
            if self._entries.get(account_id) is entry:
                self._entries.pop(account_id, None)
