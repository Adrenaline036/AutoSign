from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from autosign.core.db import AppMetadata, Database

PASSWORD_HASH_KEY = "admin_password_hash"
AUTH_VERSION_KEY = "admin_auth_version"
SESSION_COOKIE_NAME = "autosign_session"


class AuthConfigurationError(RuntimeError):
    """Raised when administrator authentication cannot be configured."""


@dataclass(frozen=True)
class AuthSession:
    token: str
    csrf_token: str
    expires_at: datetime


class AdminAuthService:
    SCRYPT_N = 2**14
    SCRYPT_R = 8
    SCRYPT_P = 1

    def __init__(self, database: Database, master_key: str, *, session_hours: int = 12) -> None:
        self._database = database
        self._signing_key = hashlib.sha256(
            b"autosign-admin-session\0" + master_key.encode("ascii")
        ).digest()
        self._session_lifetime = timedelta(hours=session_hours)

    def is_configured(self) -> bool:
        with self._database.session() as session:
            return session.get(AppMetadata, PASSWORD_HASH_KEY) is not None

    def setup(self, password: str) -> None:
        self.validate_password(password)
        password_hash = self.hash_password(password)
        with self._database.session() as session:
            if session.get(AppMetadata, PASSWORD_HASH_KEY) is not None:
                raise AuthConfigurationError("Administrator password is already configured.")
            session.add(AppMetadata(key=PASSWORD_HASH_KEY, value=password_hash))
            session.add(AppMetadata(key=AUTH_VERSION_KEY, value=secrets.token_urlsafe(18)))
            session.commit()

    def verify_password(self, password: str) -> bool:
        with self._database.session() as session:
            record = session.get(AppMetadata, PASSWORD_HASH_KEY)
        return record is not None and self.check_password(password, record.value)

    def issue_session(self) -> AuthSession:
        now = datetime.now(UTC)
        expires_at = now + self._session_lifetime
        csrf_token = secrets.token_urlsafe(24)
        payload = {
            "exp": int(expires_at.timestamp()),
            "csrf": csrf_token,
            "ver": self._auth_version(),
        }
        encoded_payload = self._b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = self._b64encode(
            hmac.digest(self._signing_key, encoded_payload.encode("ascii"), "sha256")
        )
        return AuthSession(
            token=f"{encoded_payload}.{signature}",
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    def verify_session(self, token: str | None) -> dict[str, object] | None:
        if not token:
            return None
        try:
            encoded_payload, supplied_signature = token.split(".", 1)
            expected_signature = self._b64encode(
                hmac.digest(self._signing_key, encoded_payload.encode("ascii"), "sha256")
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(self._b64decode(encoded_payload))
            if int(payload["exp"]) <= int(datetime.now(UTC).timestamp()):
                return None
            if not hmac.compare_digest(str(payload["ver"]), self._auth_version()):
                return None
            if not isinstance(payload.get("csrf"), str):
                return None
            return payload
        except (
            AuthConfigurationError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None

    def _auth_version(self) -> str:
        with self._database.session() as session:
            record = session.get(AppMetadata, AUTH_VERSION_KEY)
        if record is None:
            raise AuthConfigurationError("Administrator authentication is not configured.")
        return record.value

    @classmethod
    def validate_password(cls, password: str) -> None:
        if len(password) < 12:
            raise AuthConfigurationError("Password must contain at least 12 characters.")
        if len(password) > 200:
            raise AuthConfigurationError("Password must not exceed 200 characters.")
        if not password.strip():
            raise AuthConfigurationError("Password cannot contain only whitespace.")

    @classmethod
    def hash_password(cls, password: str) -> str:
        cls.validate_password(password)
        salt = secrets.token_bytes(16)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=cls.SCRYPT_N,
            r=cls.SCRYPT_R,
            p=cls.SCRYPT_P,
            dklen=32,
        )
        return "$".join(
            (
                "scrypt-v1",
                str(cls.SCRYPT_N),
                str(cls.SCRYPT_R),
                str(cls.SCRYPT_P),
                cls._b64encode(salt),
                cls._b64encode(derived),
            )
        )

    @classmethod
    def check_password(cls, password: str, encoded_hash: str) -> bool:
        try:
            version, n, r, p, salt, expected = encoded_hash.split("$", 5)
            if version != "scrypt-v1":
                return False
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=cls._b64decode(salt),
                n=int(n),
                r=int(r),
                p=int(p),
                dklen=32,
            )
            return hmac.compare_digest(derived, cls._b64decode(expected))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
