from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, select

from autosign.core.db import (
    Account,
    AccountNotificationChannel,
    AppMetadata,
    Database,
    NotificationChannel,
)
from autosign.core.security import SecretCipher
from autosign.core.services.accounts import AccountNotFoundError
from autosign.core.services.monitoring import (
    UPTIME_KUMA_PUSH_URL_SECRET,
    UptimeKumaPushClient,
)
from autosign.core.services.napcat import (
    NAPCAT_BASE_URL_SECRET,
    NAPCAT_SECRET_NAMES,
    NAPCAT_TARGET_ID_SECRET,
    NAPCAT_TARGET_TYPE_SECRET,
    NAPCAT_TOKEN_SECRET,
    NapCatClient,
)
from autosign.core.services.vault import VaultService
from autosign.plugin_sdk import SignResult, SignStatus

UPTIME_KUMA = "uptime_kuma"
NAPCAT = "napcat"
CHANNEL_TYPES = {UPTIME_KUMA, NAPCAT}
LEGACY_MIGRATION_KEY = "notification_channels_legacy_v1_complete"
LEGACY_MIGRATION_COMPLETE = "complete"


class NotificationChannelNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class NotificationChannelView:
    id: str
    name: str
    channel_type: str
    assigned_account_ids: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    channel_id: str
    channel_name: str
    channel_type: str
    success: bool
    message: str


class NotificationChannelService:
    def __init__(
        self,
        database: Database,
        cipher: SecretCipher,
        *,
        uptime_client: UptimeKumaPushClient | None = None,
        napcat_client: NapCatClient | None = None,
    ) -> None:
        self._database = database
        self._cipher = cipher
        self._uptime = uptime_client or UptimeKumaPushClient()
        self._napcat = napcat_client or NapCatClient()

    @staticmethod
    def _associated_data(channel_id: str) -> str:
        return f"autosign:notification-channel:{channel_id}:config:v1"

    def list(self) -> list[NotificationChannelView]:
        with self._database.session() as session:
            channels = list(
                session.scalars(
                    select(NotificationChannel).order_by(NotificationChannel.created_at)
                )
            )
            links = list(session.scalars(select(AccountNotificationChannel)))
        assignments: dict[str, list[str]] = {}
        for link in links:
            assignments.setdefault(link.channel_id, []).append(link.account_id)
        return [
            self._view(channel, assignments.get(channel.id, []))
            for channel in channels
        ]

    def get(self, channel_id: str) -> NotificationChannelView:
        with self._database.session() as session:
            channel = session.get(NotificationChannel, channel_id)
            if channel is None:
                raise NotificationChannelNotFoundError(channel_id)
            account_ids = list(
                session.scalars(
                    select(AccountNotificationChannel.account_id).where(
                        AccountNotificationChannel.channel_id == channel_id
                    )
                )
            )
            return self._view(channel, account_ids)

    def create(
        self,
        *,
        name: str,
        channel_type: str,
        config: dict[str, str],
    ) -> NotificationChannelView:
        clean_name = self._validate_name(name)
        normalized = self._validate_config(channel_type, config)
        channel = NotificationChannel(
            id=str(uuid4()),
            name=clean_name,
            channel_type=channel_type,
            encrypted_config="pending",
        )
        channel.encrypted_config = self._encrypt_config(channel.id, normalized)
        with self._database.session() as session:
            session.add(channel)
            session.commit()
            session.refresh(channel)
        return self._view(channel, [])

    def update(
        self,
        channel_id: str,
        *,
        name: str,
        config: dict[str, str] | None,
    ) -> NotificationChannelView:
        with self._database.session() as session:
            channel = session.get(NotificationChannel, channel_id)
            if channel is None:
                raise NotificationChannelNotFoundError(channel_id)
            channel.name = self._validate_name(name)
            if config is not None:
                normalized = self._validate_config(channel.channel_type, config)
                channel.encrypted_config = self._encrypt_config(channel.id, normalized)
            session.commit()
        return self.get(channel_id)

    def delete(self, channel_id: str) -> None:
        with self._database.session() as session:
            channel = session.get(NotificationChannel, channel_id)
            if channel is None:
                raise NotificationChannelNotFoundError(channel_id)
            session.delete(channel)
            session.commit()

    def assigned_to_account(self, account_id: str) -> list[NotificationChannelView]:
        with self._database.session() as session:
            if session.get(Account, account_id) is None:
                raise AccountNotFoundError(account_id)
            channels = list(
                session.scalars(
                    select(NotificationChannel)
                    .join(
                        AccountNotificationChannel,
                        AccountNotificationChannel.channel_id
                        == NotificationChannel.id,
                    )
                    .where(AccountNotificationChannel.account_id == account_id)
                    .order_by(NotificationChannel.created_at)
                )
            )
        return [self._view(channel, [account_id]) for channel in channels]

    def assign(self, account_id: str, channel_ids: list[str]) -> list[NotificationChannelView]:
        unique_ids = list(dict.fromkeys(channel_ids))
        with self._database.session() as session:
            if session.get(Account, account_id) is None:
                raise AccountNotFoundError(account_id)
            if unique_ids:
                existing_ids = set(
                    session.scalars(
                        select(NotificationChannel.id).where(
                            NotificationChannel.id.in_(unique_ids)
                        )
                    )
                )
                missing = set(unique_ids) - existing_ids
                if missing:
                    raise NotificationChannelNotFoundError(next(iter(missing)))
            session.execute(
                delete(AccountNotificationChannel).where(
                    AccountNotificationChannel.account_id == account_id
                )
            )
            session.add_all(
                [
                    AccountNotificationChannel(account_id=account_id, channel_id=channel_id)
                    for channel_id in unique_ids
                ]
            )
            session.commit()
        return self.assigned_to_account(account_id)

    async def test(self, channel_id: str) -> NotificationDelivery:
        channel, config = self._load(channel_id)
        if channel.channel_type == UPTIME_KUMA:
            return await self._send_uptime(
                channel,
                config,
                status="up",
                message="AutoSign 推送渠道测试成功",
                ping_ms=0,
            )
        return await self._send_napcat(
            channel,
            config,
            "AutoSign NapCat 推送渠道测试成功",
        )

    async def send_result(
        self,
        account_id: str,
        *,
        account_label: str,
        plugin_id: str,
        result: SignResult,
    ) -> list[NotificationDelivery]:
        deliveries: list[NotificationDelivery] = []
        status_text = {
            SignStatus.SUCCESS: "签到成功",
            SignStatus.ALREADY_SIGNED: "今日已签到",
            SignStatus.FAILED: "签到失败",
            SignStatus.INTERACTION_REQUIRED: "需要重新登录",
        }[result.status]
        executed_at = result.executed_at or datetime.now().astimezone()
        qq_lines = [
            "【AutoSign 每日签到】",
            f"账户：{account_label}",
            f"站点：{plugin_id}",
            f"结果：{status_text}",
            f"时间：{executed_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
            f"详情：{result.message}",
        ]
        if result.duration_ms is not None:
            qq_lines.append(f"耗时：{result.duration_ms} ms")
        for view in self.assigned_to_account(account_id):
            channel, config = self._load(view.id)
            if channel.channel_type == UPTIME_KUMA:
                is_up = result.status in {SignStatus.SUCCESS, SignStatus.ALREADY_SIGNED}
                deliveries.append(
                    await self._send_uptime(
                        channel,
                        config,
                        status="up" if is_up else "down",
                        message=f"{result.status.value}: {result.message}",
                        ping_ms=result.duration_ms,
                    )
                )
            else:
                deliveries.append(
                    await self._send_napcat(channel, config, "\n".join(qq_lines))
                )
        return deliveries

    def legacy_migration_complete(self) -> bool:
        with self._database.session() as session:
            record = session.get(AppMetadata, LEGACY_MIGRATION_KEY)
            return record is not None and record.value == LEGACY_MIGRATION_COMPLETE

    def migrate_legacy(self, vault: VaultService, *, force: bool = False) -> int:
        if force:
            self._clear_legacy_migration_marker()
        elif self.legacy_migration_complete():
            return 0

        migrated = self._migrate_legacy_accounts(vault)
        self._mark_legacy_migration_complete()
        return migrated

    def _migrate_legacy_accounts(self, vault: VaultService) -> int:
        migrated = 0
        with self._database.session() as session:
            accounts = list(session.scalars(select(Account).order_by(Account.created_at)))
        for account in accounts:
            names = set(vault.list_names(account.id))
            legacy: list[tuple[str, dict[str, str], set[str]]] = []
            if UPTIME_KUMA_PUSH_URL_SECRET in names:
                legacy.append(
                    (
                        UPTIME_KUMA,
                        {"push_url": vault.get(account.id, UPTIME_KUMA_PUSH_URL_SECRET)},
                        {UPTIME_KUMA_PUSH_URL_SECRET},
                    )
                )
            if NAPCAT_SECRET_NAMES.issubset(names):
                legacy.append(
                    (
                        NAPCAT,
                        {
                            "base_url": vault.get(account.id, NAPCAT_BASE_URL_SECRET),
                            "access_token": vault.get(account.id, NAPCAT_TOKEN_SECRET),
                            "target_type": vault.get(account.id, NAPCAT_TARGET_TYPE_SECRET),
                            "target_id": vault.get(account.id, NAPCAT_TARGET_ID_SECRET),
                        },
                        set(NAPCAT_SECRET_NAMES),
                    )
                )
            for channel_type, config, secret_names in legacy:
                channel = self._find_matching(channel_type, config)
                if channel is None:
                    default_name = (
                        f"Kuma · {account.label}"
                        if channel_type == UPTIME_KUMA
                        else "QQ · NapCat"
                    )
                    channel = self.create(
                        name=default_name,
                        channel_type=channel_type,
                        config=config,
                    )
                assigned_ids = [item.id for item in self.assigned_to_account(account.id)]
                if channel.id not in assigned_ids:
                    self.assign(account.id, [*assigned_ids, channel.id])
                vault.delete_many(account.id, secret_names)
                migrated += 1
        return migrated

    def _mark_legacy_migration_complete(self) -> None:
        with self._database.session() as session:
            record = session.get(AppMetadata, LEGACY_MIGRATION_KEY)
            if record is None:
                session.add(
                    AppMetadata(
                        key=LEGACY_MIGRATION_KEY,
                        value=LEGACY_MIGRATION_COMPLETE,
                    )
                )
            else:
                record.value = LEGACY_MIGRATION_COMPLETE
            session.commit()

    def _clear_legacy_migration_marker(self) -> None:
        with self._database.session() as session:
            session.execute(
                delete(AppMetadata).where(AppMetadata.key == LEGACY_MIGRATION_KEY)
            )
            session.commit()

    def _find_matching(
        self,
        channel_type: str,
        config: dict[str, str],
    ) -> NotificationChannelView | None:
        normalized = self._validate_config(channel_type, config)
        for channel in self.list():
            if channel.channel_type != channel_type:
                continue
            _, existing = self._load(channel.id)
            if existing == normalized:
                return channel
        return None

    def _load(self, channel_id: str) -> tuple[NotificationChannel, dict[str, str]]:
        with self._database.session() as session:
            channel = session.get(NotificationChannel, channel_id)
            if channel is None:
                raise NotificationChannelNotFoundError(channel_id)
            config = self._decrypt_config(channel)
            session.expunge(channel)
        return channel, config

    def _encrypt_config(self, channel_id: str, config: dict[str, str]) -> str:
        plaintext = json.dumps(config, ensure_ascii=False, sort_keys=True)
        return self._cipher.encrypt(
            plaintext,
            associated_data=self._associated_data(channel_id),
        )

    def _decrypt_config(self, channel: NotificationChannel) -> dict[str, str]:
        plaintext = self._cipher.decrypt(
            channel.encrypted_config,
            associated_data=self._associated_data(channel.id),
        )
        payload = json.loads(plaintext)
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            raise ValueError("Notification channel config is invalid.")
        return payload

    def _validate_config(
        self,
        channel_type: str,
        config: dict[str, str],
    ) -> dict[str, str]:
        if channel_type == UPTIME_KUMA:
            return {"push_url": self._uptime.validate_url(config.get("push_url", ""))}
        if channel_type == NAPCAT:
            value = self._napcat.validate_config(
                base_url=config.get("base_url", ""),
                access_token=config.get("access_token", ""),
                target_type=config.get("target_type", ""),
                target_id=config.get("target_id", ""),
            )
            return {
                "base_url": value.base_url,
                "access_token": value.access_token,
                "target_type": value.target_type,
                "target_id": value.target_id,
            }
        raise ValueError("Notification channel type must be uptime_kuma or napcat.")

    async def _send_uptime(
        self,
        channel: NotificationChannel,
        config: dict[str, str],
        *,
        status: str,
        message: str,
        ping_ms: int | None,
    ) -> NotificationDelivery:
        try:
            await self._uptime.push(
                config["push_url"],
                status=status,
                message=message,
                ping_ms=ping_ms,
            )
        except Exception as exc:
            return self._delivery(channel, False, f"推送失败：{exc}")
        return self._delivery(channel, True, "Uptime Kuma 已接受推送。")

    async def _send_napcat(
        self,
        channel: NotificationChannel,
        config: dict[str, str],
        message: str,
    ) -> NotificationDelivery:
        validated = self._napcat.validate_config(**config)
        try:
            await self._napcat.send(validated, message)
        except Exception as exc:
            return self._delivery(channel, False, f"NapCat 推送失败：{exc}")
        return self._delivery(channel, True, "NapCat 已发送 QQ 消息。")

    @staticmethod
    def _validate_name(name: str) -> str:
        clean = name.strip()
        if not clean or len(clean) > 100:
            raise ValueError("Notification channel name must contain 1-100 characters.")
        return clean

    @staticmethod
    def _view(
        channel: NotificationChannel,
        assigned_account_ids: list[str],
    ) -> NotificationChannelView:
        return NotificationChannelView(
            id=channel.id,
            name=channel.name,
            channel_type=channel.channel_type,
            assigned_account_ids=assigned_account_ids,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    @staticmethod
    def _delivery(
        channel: NotificationChannel,
        success: bool,
        message: str,
    ) -> NotificationDelivery:
        return NotificationDelivery(
            channel_id=channel.id,
            channel_name=channel.name,
            channel_type=channel.channel_type,
            success=success,
            message=message,
        )
