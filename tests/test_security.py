from __future__ import annotations

from autosign.core.security import SecretCipher


def test_cipher_uses_random_nonce_and_associated_data() -> None:
    cipher = SecretCipher(SecretCipher.generate_key())

    first = cipher.encrypt("same-value", associated_data="account:a:cookie")
    second = cipher.encrypt("same-value", associated_data="account:a:cookie")

    assert first != second
    assert cipher.decrypt(first, associated_data="account:a:cookie") == "same-value"
