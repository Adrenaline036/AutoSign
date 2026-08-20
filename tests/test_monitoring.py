from __future__ import annotations

from autosign.core.services.monitoring import UptimeKumaPushClient


def test_push_url_builder_overwrites_status_fields() -> None:
    url = UptimeKumaPushClient.build_url(
        "https://kuma.example/api/push/token?status=down&custom=keep",
        status="up",
        message="签到成功",
        ping_ms=123,
    )
    assert "status=up" in url
    assert "status=down" not in url
    assert "custom=keep" in url
    assert "ping=123" in url
