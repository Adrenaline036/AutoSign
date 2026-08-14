from __future__ import annotations

from collections import deque
from collections.abc import Callable
from time import monotonic


class LoginAttemptLimiter:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 60,
        max_clients: int = 1024,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0 or max_clients < 1:
            raise ValueError("Login limiter settings must be positive.")
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._attempts: dict[str, deque[float]] = {}

    def is_limited(self, client_key: str) -> bool:
        now = self._clock()
        self._prune(now)
        attempts = self._attempts.get(client_key)
        if attempts is not None:
            return len(attempts) >= self._max_attempts
        # Fail closed for a new source when every bounded slot is currently
        # active.  In particular, do not evict a still-limited attacker merely
        # because another source arrived.
        return len(self._attempts) >= self._max_clients

    def record_failure(self, client_key: str) -> None:
        now = self._clock()
        self._prune(now)
        attempts = self._attempts.get(client_key)
        if attempts is None:
            if len(self._attempts) >= self._max_clients:
                return
            attempts = deque()
            self._attempts[client_key] = attempts
        attempts.append(now)

    def clear(self, client_key: str) -> None:
        self._attempts.pop(client_key, None)

    @property
    def client_count(self) -> int:
        self._prune(self._clock())
        return len(self._attempts)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        expired_clients: list[str] = []
        for client_key, attempts in self._attempts.items():
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                expired_clients.append(client_key)
        for client_key in expired_clients:
            self._attempts.pop(client_key, None)
