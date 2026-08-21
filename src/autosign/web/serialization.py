from __future__ import annotations

from datetime import UTC, datetime


def aware_utc(value: datetime | None) -> datetime | None:
    """Project a database datetime into the timezone-aware Web response form."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)
