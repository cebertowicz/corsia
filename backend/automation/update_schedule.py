"""System-owned scheduled Update policy; the nightly loop owns its execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infra.database import get_db
from infra.settings import get_update_schedule, set_setting


SCHEDULE_WINDOW = timedelta(hours=1)


def _time_parts(value: str) -> tuple[int, int]:
    hour, minute = (int(part) for part in value.split(":"))
    return hour, minute


def next_local_time(now: datetime, value: str) -> datetime:
    """Return the next local occurrence of a validated HH:MM preference."""
    hour, minute = _time_parts(value)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def update_schedule_patch(payload: dict, *, now: datetime) -> dict:
    """Validate and persist an approved partial schedule update."""
    allowed = {"automatic", "time", "tonight"}
    if not payload or any(key not in allowed for key in payload):
        raise ValueError("automatic, time, or tonight is required")
    current = get_update_schedule()
    if "automatic" in payload:
        if not isinstance(payload["automatic"], bool):
            raise ValueError("automatic must be a boolean")
        current["automatic"] = payload["automatic"]
    if "time" in payload:
        value = payload["time"]
        if not isinstance(value, str):
            raise ValueError("time must use HH:MM local time")
        try:
            hour, minute = _time_parts(value)
        except ValueError as error:
            raise ValueError("time must use HH:MM local time") from error
        if not 0 <= hour <= 23 or not 0 <= minute <= 59 or len(value) != 5:
            raise ValueError("time must use HH:MM local time")
        current["time"] = value
    if "tonight" in payload:
        if not isinstance(payload["tonight"], bool):
            raise ValueError("tonight must be a boolean")
        current["tonight_at"] = (
            next_local_time(now, current["time"]).astimezone().isoformat()
            if payload["tonight"] else None
        )
    set_setting("update_schedule", current)
    return current


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def due_trigger(
    schedule: dict,
    *,
    now: datetime,
    update_available: bool,
    attempted_today: bool,
) -> str | None:
    """Return the one schedule trigger due at this tick, without launching it."""
    tonight_at = _parse(schedule.get("tonight_at"))
    if tonight_at is not None:
        due_at = tonight_at.astimezone(now.tzinfo)
        if due_at <= now < due_at + SCHEDULE_WINDOW:
            return "tonight" if update_available else None
    if not schedule.get("automatic") or not update_available or attempted_today:
        return None
    hour, _minute = _time_parts(schedule["time"])
    return "automatic" if now.hour == hour else None


def should_drop_tonight(schedule: dict, *, now: datetime) -> bool:
    """Return whether the one-hour Tonight window has expired silently."""
    tonight_at = _parse(schedule.get("tonight_at"))
    return tonight_at is not None and tonight_at.astimezone(now.tzinfo) + SCHEDULE_WINDOW <= now


def clear_tonight(schedule: dict) -> dict:
    """Clear the one-shot schedule after it is claimed or expires."""
    if schedule.get("tonight_at") is None:
        return schedule
    updated = {**schedule, "tonight_at": None}
    set_setting("update_schedule", updated)
    return updated


def update_attempted_today(*, now: datetime | None = None, db=None) -> bool:
    """Return whether any Update was acquired in the current local calendar day."""
    local_now = (now or datetime.now().astimezone()).astimezone()
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT 1 FROM self_restart_requests WHERE kind='update' "
            "AND requested_at >= ? LIMIT 1",
            (start.astimezone(timezone.utc).isoformat(),),
        ).fetchone()
        return row is not None
    finally:
        if db is None:
            connection.close()
