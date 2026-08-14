from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from autosign.core.account_operations import AccountOperationRejectedError
from autosign.core.db import Database, Schedule
from autosign.core.services.accounts import AccountService
from autosign.core.services.schedules import ScheduleCoordinator, ScheduleService
from autosign.plugin_sdk import SignResult, SignStatus


def create_schedule(tmp_path: Path) -> tuple[Database, ScheduleService, str]:
    database = Database(f"sqlite:///{(tmp_path / 'schedule.db').as_posix()}")
    database.migrate()
    account = AccountService(database).create(
        plugin_id="demo",
        label="计划测试",
        enabled=True,
        settings={},
    )
    service = ScheduleService(database, random_source=random.Random(0))
    service.upsert(
        account.id,
        enabled=True,
        daily_time="08:00",
        timezone="Asia/Shanghai",
        jitter_minutes=15,
        max_retries=2,
        retry_delay_minutes=5,
    )
    return database, service, account.id


def test_due_schedule_is_claimed_once_and_advanced(tmp_path: Path) -> None:
    database, service, account_id = create_schedule(tmp_path)
    now = datetime.now(UTC)
    with database.session() as session:
        schedule = session.scalar(select(Schedule).where(Schedule.account_id == account_id))
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=1)
        session.commit()

    claimed = service.claim_due(now)
    assert len(claimed) == 1
    assert claimed[0].account_id == account_id
    assert claimed[0].next_run_at is not None
    assert claimed[0].next_run_at > now
    assert service.claim_due(now) == []


@pytest.mark.asyncio
async def test_failed_schedule_retries_then_stops_on_success(tmp_path: Path) -> None:
    _, service, account_id = create_schedule(tmp_path)
    schedule = service.get_for_account(account_id)
    assert schedule is not None
    schedule = replace(schedule, retry_delay_minutes=0)
    attempts: list[int] = []

    async def execute(_account_id: str, trigger: str, attempt: int) -> SignResult:
        attempts.append(attempt)
        status = SignStatus.FAILED if attempt == 1 else SignStatus.SUCCESS
        return SignResult(status=status, message=trigger, verified=status is SignStatus.SUCCESS)

    coordinator = ScheduleCoordinator(service, execute)
    await coordinator._execute(schedule)

    assert attempts == [1, 2]
    assert service.get_for_account(account_id).last_status == "success"


@pytest.mark.asyncio
async def test_schedule_rejected_by_account_deletion_records_failure_without_notification(
    tmp_path: Path,
) -> None:
    _, service, account_id = create_schedule(tmp_path)
    schedule = service.get_for_account(account_id)
    assert schedule is not None
    notifications: list[SignResult] = []

    async def execute(_account_id: str, _trigger: str, _attempt: int) -> SignResult:
        raise AccountOperationRejectedError("account deletion is queued")

    async def notify(_account_id: str, result: SignResult) -> None:
        notifications.append(result)

    coordinator = ScheduleCoordinator(service, execute, notify)
    await coordinator._execute(schedule)

    assert service.get_for_account(account_id).last_status == "failed"
    assert notifications == []
