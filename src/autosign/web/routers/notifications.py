from __future__ import annotations

from fastapi import APIRouter, HTTPException

from autosign.core.services import NotificationChannelService
from autosign.core.services.accounts import AccountNotFoundError
from autosign.core.services.notifications import (
    UPTIME_KUMA,
    NotificationChannelNotFoundError,
)
from autosign.web.schemas import (
    NotificationChannelAssignmentWrite,
    NotificationChannelDeliveryRead,
    NotificationChannelRead,
    NotificationChannelWrite,
)
from autosign.web.serialization import aware_utc


def create_notifications_router(
    *,
    notifications: NotificationChannelService,
) -> APIRouter:
    router = APIRouter()

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, NotificationChannelNotFoundError):
            return HTTPException(status_code=404, detail="Unknown notification channel.")
        if isinstance(exc, AccountNotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))

    def serialize(channel) -> NotificationChannelRead:
        return NotificationChannelRead(
            id=channel.id,
            name=channel.name,
            channel_type=channel.channel_type,
            assigned_account_ids=channel.assigned_account_ids,
            created_at=aware_utc(channel.created_at),
            updated_at=aware_utc(channel.updated_at),
        )

    def config(request: NotificationChannelWrite) -> dict[str, str] | None:
        if request.channel_type == UPTIME_KUMA:
            if request.push_url is None:
                return None
            return {"push_url": request.push_url.get_secret_value()}
        if (
            request.base_url is None
            or request.access_token is None
            or request.target_type is None
            or request.target_id is None
        ):
            return None
        return {
            "base_url": request.base_url,
            "access_token": request.access_token.get_secret_value(),
            "target_type": request.target_type,
            "target_id": request.target_id,
        }

    @router.get("/api/v1/notification-channels", response_model=list[NotificationChannelRead])
    async def list_notification_channels() -> list[NotificationChannelRead]:
        return [serialize(channel) for channel in notifications.list()]

    @router.post(
        "/api/v1/notification-channels",
        response_model=NotificationChannelRead,
        status_code=201,
    )
    async def create_notification_channel(
        request: NotificationChannelWrite,
    ) -> NotificationChannelRead:
        channel_config = config(request)
        if channel_config is None:
            raise HTTPException(status_code=400, detail="推送渠道配置未填写完整。")
        try:
            channel = notifications.create(
                name=request.name,
                channel_type=request.channel_type,
                config=channel_config,
            )
            return serialize(channel)
        except ValueError as exc:
            raise error(exc) from exc

    @router.put(
        "/api/v1/notification-channels/{channel_id}",
        response_model=NotificationChannelRead,
    )
    async def update_notification_channel(
        channel_id: str,
        request: NotificationChannelWrite,
    ) -> NotificationChannelRead:
        try:
            existing = notifications.get(channel_id)
            if existing.channel_type != request.channel_type:
                raise ValueError("推送渠道类型不能修改，请新建另一个渠道。")
            channel = notifications.update(
                channel_id,
                name=request.name,
                config=config(request),
            )
            return serialize(channel)
        except (NotificationChannelNotFoundError, ValueError) as exc:
            raise error(exc) from exc

    @router.delete("/api/v1/notification-channels/{channel_id}", status_code=204)
    async def delete_notification_channel(channel_id: str) -> None:
        try:
            notifications.delete(channel_id)
        except NotificationChannelNotFoundError as exc:
            raise error(exc) from exc

    @router.post(
        "/api/v1/notification-channels/{channel_id}/test",
        response_model=NotificationChannelDeliveryRead,
    )
    async def test_notification_channel(
        channel_id: str,
    ) -> NotificationChannelDeliveryRead:
        try:
            delivery = await notifications.test(channel_id)
            if not delivery.success:
                raise HTTPException(status_code=502, detail=delivery.message)
            return NotificationChannelDeliveryRead(
                channel_id=delivery.channel_id,
                channel_name=delivery.channel_name,
                channel_type=delivery.channel_type,
                success=delivery.success,
                message=delivery.message,
            )
        except NotificationChannelNotFoundError as exc:
            raise error(exc) from exc

    @router.put(
        "/api/v1/accounts/{account_id}/notification-channels",
        response_model=list[NotificationChannelRead],
    )
    async def assign_notification_channels(
        account_id: str,
        request: NotificationChannelAssignmentWrite,
    ) -> list[NotificationChannelRead]:
        try:
            return [
                serialize(channel)
                for channel in notifications.assign(account_id, request.channel_ids)
            ]
        except (AccountNotFoundError, NotificationChannelNotFoundError) as exc:
            raise error(exc) from exc

    return router
