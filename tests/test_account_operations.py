from __future__ import annotations

import asyncio

import pytest

from autosign.core.account_operations import (
    AccountOperationGate,
    AccountOperationRejectedError,
)


@pytest.mark.asyncio
async def test_account_uses_are_serial_and_idle_entries_are_reclaimed() -> None:
    gate = AccountOperationGate()
    order: list[str] = []
    first_ready = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        async with gate.use("account-1"):
            order.append("first-start")
            first_ready.set()
            await release_first.wait()
            order.append("first-end")

    async def second() -> None:
        await first_ready.wait()
        async with gate.use("account-1"):
            order.append("second")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_ready.wait()
    await asyncio.sleep(0)
    assert order == ["first-start"]
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert order == ["first-start", "first-end", "second"]
    assert await gate.entry_count() == 0


@pytest.mark.asyncio
async def test_delete_waits_for_existing_use_and_rejects_new_use() -> None:
    gate = AccountOperationGate()
    release_use = asyncio.Event()
    use_started = asyncio.Event()
    deleted = asyncio.Event()

    async def use() -> None:
        async with gate.use("account-1"):
            use_started.set()
            await release_use.wait()

    async def delete() -> None:
        async with gate.delete("account-1"):
            deleted.set()

    use_task = asyncio.create_task(use())
    await use_started.wait()
    delete_task = asyncio.create_task(delete())
    await asyncio.sleep(0)

    with pytest.raises(AccountOperationRejectedError):
        async with gate.use("account-1"):
            pass
    assert deleted.is_set() is False

    release_use.set()
    await asyncio.gather(use_task, delete_task)
    assert deleted.is_set() is True
    assert await gate.entry_count() == 0


@pytest.mark.asyncio
async def test_failed_delete_restores_account_operations() -> None:
    gate = AccountOperationGate()
    with pytest.raises(RuntimeError, match="database busy"):
        async with gate.delete("account-1"):
            raise RuntimeError("database busy")

    async with gate.use("account-1"):
        pass
    assert await gate.entry_count() == 0
