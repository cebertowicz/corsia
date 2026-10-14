"""System-owned scheduled Update policy; the nightly loop owns its execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infra.database import get_db, write_transaction
from infra.settings import get_update_schedule, parse_update_schedule_time, set_setting


SCHEDULE_WINDOW = timedelta(hours=1)


def next_local_time(now: datetime, value: str) -> datetime:
    """Return the next local occurrence of a validated HH:MM preference."""
    hour, minute = parse_update_schedule_time(value)
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
            parse_update_schedule_time(value)
        except ValueError as error:
            raise ValueError("time must use HH:MM local time") from error
    with write_transaction(None, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if "automatic" in payload:
            current["automatic"] = payload["automatic"]
        if "time" in payload:
            current["time"] = payload["time"]
            if current.get("tonight_at") is not None:
                current["tonight_at"] = next_local_time(
                    now, current["time"]
                ).astimezone().isoformat()
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


def _tonight_due_at(schedule: dict, now: datetime) -> datetime | None:
    tonight_at = _parse(schedule.get("tonight_at"))
    if tonight_at is None:
        return None
    scheduled_night = tonight_at.astimezone(now.tzinfo)
    hour, minute = parse_update_schedule_time(schedule["time"])
    return scheduled_night.replace(hour=hour, minute=minute, second=0, microsecond=0)


def possible_trigger(
    schedule: dict, *, now: datetime, attempted_today: bool, update_available: bool | None = None
) -> str | None:
    """Return the one due trigger, optionally after the remote status check."""
    if update_available is False:
        return None
    tonight_due_at = _tonight_due_at(schedule, now)
    if tonight_due_at is not None and tonight_due_at <= now < tonight_due_at + SCHEDULE_WINDOW:
        return "tonight"
    if not schedule.get("automatic") or attempted_today:
        return None
    hour, minute = parse_update_schedule_time(schedule["time"])
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return "automatic" if due_at <= now < due_at + SCHEDULE_WINDOW else None


def should_drop_tonight(schedule: dict, *, now: datetime) -> bool:
    """Return whether the one-hour Tonight window has expired silently."""
    due_at = _tonight_due_at(schedule, now)
    return due_at is not None and due_at + SCHEDULE_WINDOW <= now


def claim_tonight(schedule: dict) -> bool:
    """Atomically consume the exact Tonight choice read by this tick."""
    expected = schedule.get("tonight_at")
    if expected is None:
        return False
    with write_transaction(None, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if current.get("tonight_at") != expected:
            return False
        current["tonight_at"] = None
        set_setting("update_schedule", current, db=connection)
        return True


def clear_tonight(schedule: dict) -> dict:
    """Drop an expired Tonight choice only if this tick still owns it."""
    claim_tonight(schedule)
    return get_update_schedule()


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
