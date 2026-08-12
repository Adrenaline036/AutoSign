from __future__ import annotations

from autosign.core.login_limiter import LoginAttemptLimiter


def test_login_limiter_window_success_cleanup_and_capacity() -> None:
    now = 100.0
    limiter = LoginAttemptLimiter(
        max_attempts=2,
        window_seconds=60,
        max_clients=3,
        clock=lambda: now,
    )

    limiter.record_failure("one")
    limiter.record_failure("one")
    assert limiter.is_limited("one") is True
    limiter.clear("one")
    assert limiter.is_limited("one") is False

    for client in ("one", "two", "three"):
        limiter.record_failure(client)
    assert limiter.client_count == 3
    assert limiter.is_limited("four") is True

    now += 61
    assert limiter.is_limited("four") is False
    assert limiter.client_count == 0


def test_login_limiter_stays_bounded_for_many_sources() -> None:
    limiter = LoginAttemptLimiter(max_clients=1024, clock=lambda: 10.0)
    for index in range(10_000):
        client = f"198.51.100.{index}"
        if not limiter.is_limited(client):
            limiter.record_failure(client)

    assert limiter.client_count == 1024
