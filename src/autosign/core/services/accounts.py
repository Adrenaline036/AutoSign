from __future__ import annotations

from typing import Any

from sqlalchemy import select

from autosign.core.db import Account, Database


class AccountNotFoundError(LookupError):
    pass


class AccountService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list(self) -> list[Account]:
        with self._database.session() as session:
            return list(session.scalars(select(Account).order_by(Account.created_at)))

    def get(self, account_id: str) -> Account:
        with self._database.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(f"Unknown account: {account_id}")
            return account

    def create(
        self,
        *,
        plugin_id: str,
        label: str,
        enabled: bool,
        settings: dict[str, Any],
    ) -> Account:
        account = Account(
            plugin_id=plugin_id,
            label=label,
            enabled=enabled,
            settings_json=settings,
        )
        with self._database.session() as session:
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def update(
        self,
        account_id: str,
        *,
        label: str | None = None,
        enabled: bool | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Account:
        with self._database.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(f"Unknown account: {account_id}")
            if label is not None:
                account.label = label
            if enabled is not None:
                account.enabled = enabled
            if settings is not None:
                account.settings_json = settings
            session.commit()
            session.refresh(account)
            return account

    def delete(self, account_id: str, *, confirm_label: str) -> None:
        with self._database.session() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(f"Unknown account: {account_id}")
            if confirm_label != account.label:
                raise ValueError("The confirmation label does not match the account label.")
            session.delete(account)
            session.commit()

