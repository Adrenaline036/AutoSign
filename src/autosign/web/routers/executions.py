from __future__ import annotations

from fastapi import APIRouter, Query

from autosign.core.services import ExecutionService
from autosign.web.schemas import ExecutionRead
from autosign.web.serialization import aware_utc


def create_executions_router(*, executions: ExecutionService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/executions", response_model=list[ExecutionRead])
    async def list_executions(
        account_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[ExecutionRead]:
        return [
            ExecutionRead(
                id=record.id,
                account_id=record.account_id,
                account_label=record.account_label,
                plugin_id=record.plugin_id,
                status=record.status,
                message=record.message,
                verified=record.verified,
                started_at=aware_utc(record.started_at),
                finished_at=aware_utc(record.finished_at),
                duration_ms=record.duration_ms,
                details=record.details,
            )
            for record in executions.list(account_id=account_id, limit=limit)
        ]

    return router
