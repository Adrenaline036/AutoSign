from __future__ import annotations

import asyncio

import pytest

from autosign.core.capacity import BrowserCapacityClosedError, BrowserCapacityGate


@pytest.mark.asyncio
async def test_capacity_pools_are_bounded_and_independent() -> None:
    gate = BrowserCapacityGate(automation_limit=2, interactive_limit=1)
    first = await gate.acquire("automation")
    second = await gate.acquire("automation")
    waiting = asyncio.create_task(gate.acquire("automation"))
    interactive = await gate.acquire("interactive")
    await asyncio.sleep(0)

    snapshot = await gate.snapshot()
    assert snapshot.automation_active == 2
    assert snapshot.automation_waiting == 1
    assert snapshot.interactive_active == 1
    assert waiting.done() is False

    await first.release()
    third = await asyncio.wait_for(waiting, timeout=1)
    assert (await gate.snapshot()).automation_active == 2

    await second.release()
    await third.release()
    await interactive.release()
    assert (await gate.snapshot()).automation_active == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_capacity() -> None:
    gate = BrowserCapacityGate(automation_limit=1, interactive_limit=1)
    active = await gate.acquire("automation")
    waiting = asyncio.create_task(gate.acquire("automation"))
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await active.release()

    snapshot = await gate.snapshot()
    assert snapshot.automation_active == 0
    assert snapshot.automation_waiting == 0


@pytest.mark.asyncio
async def test_closing_gate_rejects_waiters_and_drains_active_tokens() -> None:
    gate = BrowserCapacityGate(automation_limit=1, interactive_limit=1)
    active = await gate.acquire("automation")
    waiting = asyncio.create_task(gate.acquire("automation"))
    await asyncio.sleep(0)

    await gate.begin_close()
    with pytest.raises(BrowserCapacityClosedError):
        await waiting
    with pytest.raises(BrowserCapacityClosedError):
        await gate.acquire("interactive")

    drain = asyncio.create_task(gate.wait_drained())
    await asyncio.sleep(0)
    assert drain.done() is False
    await active.release()
    await asyncio.wait_for(drain, timeout=1)
    await active.release()  # idempotent

    snapshot = await gate.snapshot()
    assert snapshot.closing is True
    assert snapshot.automation_active == 0
