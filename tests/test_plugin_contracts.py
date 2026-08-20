from __future__ import annotations

import pytest
from pydantic import ValidationError

from autosign.plugin_sdk import SignResult, SignStatus


@pytest.mark.parametrize(
    ("status", "verified"),
    [
        (SignStatus.SUCCESS, True),
        (SignStatus.ALREADY_SIGNED, True),
        (SignStatus.FAILED, False),
        (SignStatus.INTERACTION_REQUIRED, False),
    ],
)
def test_sign_result_accepts_consistent_verification(
    status: SignStatus,
    verified: bool,
) -> None:
    result = SignResult(status=status, message="contract test", verified=verified)

    assert result.status is status
    assert result.verified is verified


@pytest.mark.parametrize(
    ("status", "verified"),
    [
        (SignStatus.SUCCESS, False),
        (SignStatus.ALREADY_SIGNED, False),
        (SignStatus.FAILED, True),
        (SignStatus.INTERACTION_REQUIRED, True),
    ],
)
def test_sign_result_rejects_inconsistent_verification(
    status: SignStatus,
    verified: bool,
) -> None:
    with pytest.raises(ValidationError, match="verified must be true only"):
        SignResult(status=status, message="contract test", verified=verified)


def test_sign_result_model_copy_accepts_consistent_metadata_update() -> None:
    result = SignResult(
        status=SignStatus.SUCCESS,
        message="contract test",
        verified=True,
    )

    copied = result.model_copy(update={"plugin_id": "demo", "account_id": "account-1"})

    assert copied.plugin_id == "demo"
    assert copied.account_id == "account-1"
    assert copied.status is SignStatus.SUCCESS
    assert copied.verified is True


@pytest.mark.parametrize(
    ("result", "update"),
    [
        (
            SignResult(
                status=SignStatus.SUCCESS,
                message="contract test",
                verified=True,
            ),
            {"verified": False},
        ),
        (
            SignResult(
                status=SignStatus.FAILED,
                message="contract test",
                verified=False,
            ),
            {"status": SignStatus.SUCCESS},
        ),
    ],
)
def test_sign_result_model_copy_rejects_inconsistent_update(
    result: SignResult,
    update: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="verified must be true only"):
        result.model_copy(update=update)
