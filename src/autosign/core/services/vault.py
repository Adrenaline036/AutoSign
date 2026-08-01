from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from autosign.core.db import Account, AccountSecret, AppMetadata, Database
from autosign.core.security import SecretCipher, SecretDecryptionError
from autosign.core.services.accounts import AccountNotFoundError

KEY_CHECK_NAME = "master_key_check"
KEY_CHECK_PLAINTEXT = "autosign-master-key-ok-v1"
KEY_CHECK_AAD = "autosign:key-check:v1"


class VaultService:
    def __init__(self, database: Database, cipher: SecretCipher) -> None:
        self._database = database
        self._cipher = cipher

    @staticmethod
    def _associated_data(account_id: str, name: str) -> str:
        return f"autosign:account:{account_id}:secret:{name}:v1"

    def initialize_key_check(self) -> None:
        with self._database.session() as session:
            record = session.get(AppMetadata, KEY_CHECK_NAME)
            if record is None:
                record = AppMetadata(
                    key=KEY_CHECK_NAME,
                    value=self._cipher.encrypt(
                        KEY_CHECK_PLAINTEXT,
                        associated_data=KEY_CHECK_AAD,
                    ),
                )
                session.add(record)
                session.commit()
                return
            plaintext = self._cipher.decrypt(record.value, associated_data=KEY_CHECK_AAD)
            if plaintext != KEY_CHECK_PLAINTEXT:
                raise SecretDecryptionError("The database master-key check value is invalid.")

    def for_account(self, account_id: str) -> AccountSecretAccessor:
        return AccountSecretAccessor(vault=self, account_id=account_id)

    def list_names(self, account_id: str) -> list[str]:
        with self._database.session() as session:
            self._require_account(session, account_id)
            statement = (
                select(AccountSecret.name)
                .where(AccountSecret.account_id == account_id)
                .order_by(AccountSecret.name)
            )
            return list(session.scalars(statement))

    def set(self, account_id: str, name: str, value: str) -> None:
        with self._database.session() as session:
            self._require_account(session, account_id)
            encrypted = self._cipher.encrypt(
                value,
                associated_data=self._associated_data(account_id, name),
            )
            record = session.get(AccountSecret, (account_id, name))
            if record is None:
                record = AccountSecret(
                    account_id=account_id,
                    name=name,
                    encrypted_value=encrypted,
                )
                session.add(record)
            else:
                record.encrypted_value = encrypted
                record.updated_at = datetime.now(UTC)
            session.commit()

    def get(self, account_id: str, name: str) -> str:
        with self._database.session() as session:
            self._require_account(session, account_id)
            record = session.get(AccountSecret, (account_id, name))
            if record is None:
                raise LookupError(f"Unknown secret: {name}")
            return self._cipher.decrypt(
                record.encrypted_value,
                associated_data=self._associated_data(account_id, name),
            )

    def delete(self, account_id: str, name: str) -> None:
        with self._database.session() as session:
            self._require_account(session, account_id)
            record = session.get(AccountSecret, (account_id, name))
            if record is None:
                raise LookupError(f"Unknown secret: {name}")
            session.delete(record)
            session.commit()

    @staticmethod
    def _require_account(session, account_id: str) -> None:
        if session.get(Account, account_id) is None:
            raise AccountNotFoundError(f"Unknown account: {account_id}")


@dataclass(frozen=True, slots=True)
class AccountSecretAccessor:
    vault: VaultService
    account_id: str

    def get(self, name: str) -> str:
        return self.vault.get(self.account_id, name)

    def names(self) -> list[str]:
        return self.vault.list_names(self.account_id)
