"""UTC time helpers that preserve CORSIA's naive timestamp storage shape."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return a naive UTC datetime for legacy DB timestamp comparisons."""
    return datetime.now(UTC).replace(tzinfo=None)


def utc_now_iso() -> str:
    """Return a naive UTC ISO timestamp string for existing DB rows."""
    return utc_now().isoformat()


def utc_storage_datetime(value: str | datetime) -> datetime:
    """One stored or API UTC timestamp as an aware datetime; naive means UTC."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def utc_storage_iso_to_api(value: str | datetime | None) -> str | None:
    """Render one stored UTC timestamp as explicit RFC 3339 UTC."""
    if value is None:
        return None
    return utc_storage_datetime(value).astimezone(UTC).isoformat().replace("+00:00", "Z")
