from __future__ import annotations

import pytest

from autosign.core.services.napcat import NapCatClient


def test_napcat_config_validation() -> None:
    with pytest.raises(ValueError, match="access token"):
        NapCatClient.validate_config(
            base_url="http://napcat.example:3000",
            access_token="",
            target_type="private",
            target_id="123456789",
        )
    with pytest.raises(ValueError, match="5-20 digits"):
        NapCatClient.validate_config(
            base_url="http://napcat.example:3000",
            access_token="token",
            target_type="group",
            target_id="not-a-group",
        )
