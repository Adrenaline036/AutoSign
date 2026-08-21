from __future__ import annotations

import pytest
from pydantic import ValidationError

from autosign.plugin_sdk import (
    AutoSignPlugin,
    PluginContext,
    PluginManifest,
    SessionState,
    SignResult,
    SignStatus,
)


class MinimalPlugin(AutoSignPlugin):
    manifest = PluginManifest(id="minimal", name="Minimal", version="1.0.0")

    async def sign(self, context: PluginContext) -> SignResult:
        return SignResult(status=SignStatus.SUCCESS, message="ok", verified=True)


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


@pytest.mark.asyncio
async def test_plugin_sdk_v1_defaults_deprecated_session_check_to_unknown() -> None:
    result = await MinimalPlugin().check_session(
        PluginContext(account_id="account-1", account_label="Minimal")
    )

    assert result.state is SessionState.UNKNOWN
    assert "validated when the plugin executes" in result.message


def test_plugin_context_marks_uninjected_http_field_as_deprecated() -> None:
    field = PluginContext.__dataclass_fields__["http"]

    assert "never injected" in field.metadata["deprecated"]
