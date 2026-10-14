"""System-owned scheduled Update policy; the nightly loop owns its execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infra.database import get_db, write_transaction
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


def update_schedule_patch(
    payload: dict, *, now: datetime, user_id: str | None = None
) -> dict:
    """Validate and persist an approved partial schedule update."""
    allowed = {"automatic", "time", "tonight"}
    if not payload or any(key not in allowed for key in payload):
        raise ValueError("automatic, time, or tonight is required")
    if "automatic" in payload and not isinstance(payload["automatic"], bool):
        raise ValueError("automatic must be a boolean")
    if "tonight" in payload and not isinstance(payload["tonight"], bool):
        raise ValueError("tonight must be a boolean")
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
    with write_transaction(None, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if "automatic" in payload:
            current["automatic"] = payload["automatic"]
        if "time" in payload:
            current["time"] = payload["time"]
        if "tonight" in payload:
            current["tonight_at"] = (
                next_local_time(now, current["time"]).astimezone().isoformat()
                if payload["tonight"] else None
            )
        if user_id is not None and ({"automatic", "tonight"} & payload.keys()):
            current["owner_id"] = user_id
        set_setting("update_schedule", current, db=connection)
        result = {key: current[key] for key in ("automatic", "time", "tonight_at")}
    return result


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
        scheduled_night = tonight_at.astimezone(now.tzinfo)
        hour, minute = _time_parts(schedule["time"])
        due_at = scheduled_night.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due_at <= now < due_at + SCHEDULE_WINDOW:
            return "tonight" if update_available else None
    if not schedule.get("automatic") or not update_available or attempted_today:
        return None
    # Same one-hour window as Tonight, from the exact chosen minute. The window
    # ends at local midnight: the attempted-today guard resets there.
    hour, minute = _time_parts(schedule["time"])
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return "automatic" if due_at <= now < due_at + SCHEDULE_WINDOW else None


def should_drop_tonight(schedule: dict, *, now: datetime) -> bool:
    """Return whether the one-hour Tonight window has expired silently."""
    tonight_at = _parse(schedule.get("tonight_at"))
    if tonight_at is None:
        return False
    scheduled_night = tonight_at.astimezone(now.tzinfo)
    hour, minute = _time_parts(schedule["time"])
    due_at = scheduled_night.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return due_at + SCHEDULE_WINDOW <= now


def clear_tonight(schedule: dict) -> dict:
    """Clear Tonight only if the tick still owns the value it read."""
    expected = schedule.get("tonight_at")
    if expected is None:
        return schedule
    with write_transaction(None, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if current.get("tonight_at") == expected:
            current["tonight_at"] = None
            set_setting("update_schedule", current, db=connection)
        return get_update_schedule(db=connection)


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
