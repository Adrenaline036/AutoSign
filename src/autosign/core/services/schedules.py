from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from autosign.core.db import Account, Database, Schedule
from autosign.core.services.accounts import AccountNotFoundError
from autosign.plugin_sdk import SignResult, SignStatus

ScheduleExecutor = Callable[[str, str, int], Awaitable[SignResult]]
ScheduleNotifier = Callable[[str, SignResult], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScheduleView:
    id: str
    account_id: str
    account_label: str
    enabled: bool
    daily_time: str
    timezone: str
    jitter_minutes: int
    max_retries: int
    retry_delay_minutes: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None


class ScheduleService:
    def __init__(self, database: Database, *, random_source: random.Random | None = None) -> None:
        self._database = database
        self._random = random_source or random.SystemRandom()

    def list(self) -> list[ScheduleView]:
        statement = (
            select(Schedule, Account)
            .join(Account, Schedule.account_id == Account.id)
            .order_by(Account.created_at, Schedule.created_at)
        )
        with self._database.session() as session:
            rows = session.execute(statement).all()
        return [self._view(schedule, account) for schedule, account in rows]

    def get_for_account(self, account_id: str) -> ScheduleView | None:
        statement = (
            select(Schedule, Account)
            .join(Account, Schedule.account_id == Account.id)
            .where(Schedule.account_id == account_id)
            .order_by(Schedule.created_at)
        )
        with self._database.session() as session:
            row = session.execute(statement).first()
        return self._view(*row) if row is not None else None

    def upsert(
        self,
        account_id: str,
        *,
        enabled: bool,
        daily_time: str,
        timezone: str,
        jitter_minutes: int,
        max_retries: int,
        retry_delay_minutes: int,
    ) -> ScheduleView:
        hour, minute = self._parse_daily_time(daily_time)
        self._timezone(timezone)
        cron_expression = f"{minute} {hour} * * *"
        with self._database.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(f"Unknown account: {account_id}")
            schedule = session.scalar(
                select(Schedule)
                .where(Schedule.account_id == account_id)
                .order_by(Schedule.created_at)
            )
            if schedule is None:
                schedule = Schedule(account_id=account_id, name="每日自动签到")
                session.add(schedule)
            schedule.cron_expression = cron_expression
            schedule.timezone = timezone
            schedule.jitter_seconds = jitter_minutes * 60
            schedule.max_retries = max_retries
            schedule.retry_delay_seconds = retry_delay_minutes * 60
            schedule.enabled = enabled
            schedule.next_run_at = (
                self._next_run(schedule, datetime.now(UTC)) if enabled else None
            )
            session.commit()
            session.refresh(schedule)
            return self._view(schedule, account)

    def delete_for_account(self, account_id: str) -> None:
        with self._database.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(f"Unknown account: {account_id}")
            schedules = list(
                session.scalars(select(Schedule).where(Schedule.account_id == account_id))
            )
            for schedule in schedules:
                session.delete(schedule)
            session.commit()

    def initialize(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        with self._database.session() as session:
            schedules = list(
                session.scalars(
                    select(Schedule).where(
                        Schedule.enabled.is_(True),
                        Schedule.next_run_at.is_(None),
                    )
                )
            )
            for schedule in schedules:
                schedule.next_run_at = self._next_run(schedule, now)
            session.commit()

    def claim_due(self, now: datetime | None = None) -> list[ScheduleView]:
        now = now or datetime.now(UTC)
        statement = (
            select(Schedule, Account)
            .join(Account, Schedule.account_id == Account.id)
            .where(
                Schedule.enabled.is_(True),
                Account.enabled.is_(True),
                Schedule.next_run_at.is_not(None),
                Schedule.next_run_at <= now,
            )
            .order_by(Schedule.next_run_at)
        )
        with self._database.session() as session:
            rows = session.execute(statement).all()
            views: list[ScheduleView] = []
            for schedule, account in rows:
                schedule.last_run_at = now
                schedule.next_run_at = self._next_run(schedule, now)
                views.append(self._view(schedule, account))
            session.commit()
        return views

    def record_status(self, schedule_id: str, status: str) -> None:
        with self._database.session() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is not None:
                schedule.last_status = status
                session.commit()

    def _next_run(self, schedule: Schedule, now: datetime) -> datetime:
        now = self._aware_utc(now)
        zone = self._timezone(schedule.timezone)
        hour, minute = self._cron_time(schedule.cron_expression)
        local_now = now.astimezone(zone)
        local_run = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_run <= local_now:
            local_run += timedelta(days=1)
        jitter = self._random.randint(0, max(0, schedule.jitter_seconds))
        return (local_run + timedelta(seconds=jitter)).astimezone(UTC)

    @staticmethod
    def _view(schedule: Schedule, account: Account) -> ScheduleView:
        hour, minute = ScheduleService._cron_time(schedule.cron_expression)
        return ScheduleView(
            id=schedule.id,
            account_id=schedule.account_id,
            account_label=account.label,
            enabled=schedule.enabled,
            daily_time=f"{hour:02d}:{minute:02d}",
            timezone=schedule.timezone,
            jitter_minutes=schedule.jitter_seconds // 60,
            max_retries=schedule.max_retries,
            retry_delay_minutes=schedule.retry_delay_seconds // 60,
            next_run_at=schedule.next_run_at,
            last_run_at=schedule.last_run_at,
            last_status=schedule.last_status,
        )

    @staticmethod
    def _parse_daily_time(value: str) -> tuple[int, int]:
        try:
            hour_text, minute_text = value.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("Daily time must use HH:MM.") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Daily time must use HH:MM.")
        return hour, minute

    @staticmethod
    def _cron_time(expression: str) -> tuple[int, int]:
        fields = expression.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(f"Unsupported schedule expression: {expression}")
        minute, hour = int(fields[0]), int(fields[1])
        return hour, minute

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {name}") from exc

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ScheduleCoordinator:
    def __init__(
        self,
        schedules: ScheduleService,
        executor: ScheduleExecutor,
        notifier: ScheduleNotifier | None = None,
        *,
        poll_seconds: float = 15,
    ) -> None:
        self._schedules = schedules
        self._executor = executor
        self._notifier = notifier
        self._poll_seconds = poll_seconds
        self._loop_task: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[None]] = set()
        self._logger = logging.getLogger("autosign.scheduler")

    def start(self) -> None:
        self._schedules.initialize()
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_loop())

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_job_count(self) -> int:
        return sum(not job.done() for job in self._jobs)

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for job in self._jobs:
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        self._jobs.clear()

    async def poll_once(self) -> None:
        for schedule in self._schedules.claim_due():
            job = asyncio.create_task(self._execute(schedule))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                self._logger.exception("Schedule polling failed")
            await asyncio.sleep(self._poll_seconds)

    async def _execute(self, schedule: ScheduleView) -> None:
        result: SignResult | None = None
        try:
            for attempt in range(1, schedule.max_retries + 2):
                result = await self._executor(schedule.account_id, "schedule", attempt)
                if result.status is not SignStatus.FAILED:
                    break
                if attempt <= schedule.max_retries:
                    await asyncio.sleep(schedule.retry_delay_minutes * 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Scheduled execution crashed for account %s",
                schedule.account_id,
            )
            self._schedules.record_status(schedule.id, SignStatus.FAILED.value)
            return
        if result is not None:
            self._schedules.record_status(schedule.id, result.status.value)
            if self._notifier is not None:
                try:
                    await self._notifier(schedule.account_id, result)
                except Exception:
                    self._logger.exception(
                        "Schedule result notification crashed for account %s",
                        schedule.account_id,
                    )
