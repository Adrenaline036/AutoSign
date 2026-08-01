from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretConfigurationError(RuntimeError):
    """Raised when the configured master key is malformed."""


class SecretDecryptionError(RuntimeError):
    """Raised when encrypted data cannot be opened with the configured key."""


class SecretCipher:
    NONCE_BYTES = 12

    def __init__(self, encoded_key: str) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise SecretConfigurationError("AUTOSIGN_MASTER_KEY is not valid base64.") from exc
        if len(key) != 32:
            raise SecretConfigurationError("AUTOSIGN_MASTER_KEY must decode to exactly 32 bytes.")
        self._cipher = AESGCM(key)

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")

    def encrypt(self, value: str, *, associated_data: str) -> str:
        nonce = os.urandom(self.NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            value.encode("utf-8"),
            associated_data.encode("utf-8"),
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, value: str, *, associated_data: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value.encode("ascii"))
            nonce = payload[: self.NONCE_BYTES]
            ciphertext = payload[self.NONCE_BYTES :]
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                associated_data.encode("utf-8"),
            )
        except (ValueError, UnicodeEncodeError, binascii.Error, InvalidTag) as exc:
            raise SecretDecryptionError(
                "Encrypted data cannot be decrypted with the configured master key."
            ) from exc
        return plaintext.decode("utf-8")

