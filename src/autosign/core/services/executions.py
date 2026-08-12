from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from sqlalchemy import select

from autosign.core.db import Account, Database, ExecutionRecord
from autosign.core.runner import PluginRunner
from autosign.plugin_sdk import BrowserAutomation, SecretAccessor, SignResult, SignStatus


@dataclass(frozen=True, slots=True)
class ExecutionView:
    id: str
    account_id: str
    account_label: str
    plugin_id: str
    status: str
    message: str
    verified: bool
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    details: dict[str, Any]


class ExecutionService:
    def __init__(self, database: Database, runner: PluginRunner) -> None:
        self._database = database
        self._runner = runner
        self._logger = logging.getLogger("autosign.executions")

    async def execute(
        self,
        plugin_id: str,
        *,
        account_id: str,
        account_label: str,
        settings: dict[str, Any],
        secrets: SecretAccessor,
        browser: BrowserAutomation | None = None,
        trigger: str = "manual",
        attempt: int = 1,
    ) -> SignResult:
        started_at = datetime.now(UTC)
        started = perf_counter()
        try:
            result = await self._runner.execute(
                plugin_id,
                account_id=account_id,
                account_label=account_label,
                settings=settings,
                secrets=secrets,
                browser=browser,
            )
        except Exception as exc:
            self._logger.exception(
                "Account execution failed for account %s using plugin %s",
                account_id,
                plugin_id,
            )
            result = SignResult(
                status=SignStatus.FAILED,
                message=f"签到执行发生错误：{exc}",
                verified=False,
                details={"error_type": type(exc).__name__},
                plugin_id=plugin_id,
                account_id=account_id,
                executed_at=datetime.now(UTC),
                duration_ms=round((perf_counter() - started) * 1000),
            )
        finished_at = result.executed_at or datetime.now(UTC)
        result.details = {
            **result.details,
            "trigger": trigger,
            "attempt": attempt,
        }
        record_id = self._record(
            account_id=account_id,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
        )
        result.details["execution_record_id"] = record_id
        return result

    def list(self, *, account_id: str | None = None, limit: int = 50) -> list[ExecutionView]:
        statement = (
            select(ExecutionRecord, Account)
            .join(Account, ExecutionRecord.account_id == Account.id)
            .order_by(ExecutionRecord.started_at.desc())
            .limit(limit)
        )
        if account_id is not None:
            statement = statement.where(ExecutionRecord.account_id == account_id)
        with self._database.session() as session:
            rows = session.execute(statement).all()
        return [
            ExecutionView(
                id=record.id,
                account_id=record.account_id,
                account_label=account.label,
                plugin_id=account.plugin_id,
                status=record.status,
                message=record.message,
                verified=record.verified,
                started_at=record.started_at,
                finished_at=record.finished_at,
                duration_ms=record.duration_ms,
                details=record.details_json,
            )
            for record, account in rows
        ]

    def _record(
        self,
        *,
        account_id: str,
        result: SignResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> str:
        record = ExecutionRecord(
            account_id=account_id,
            status=result.status.value,
            message=result.message,
            verified=result.verified,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=result.duration_ms,
            details_json=result.details,
        )
        with self._database.session() as session:
            session.add(record)
            session.commit()
            return record.id

    def annotate(self, record_id: str, details: dict[str, Any]) -> None:
        with self._database.session() as session:
            record = session.get(ExecutionRecord, record_id)
            if record is None:
                return
            record.details_json = {**record.details_json, **details}
            session.commit()
