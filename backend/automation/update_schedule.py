"""System-owned scheduled Update policy and nightly tick lifecycle."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from infra.database import get_db, write_transaction
from infra.settings import get_update_schedule, parse_update_schedule_time, set_setting


SCHEDULE_WINDOW = timedelta(hours=1)
_SCHEDULE_NOTICE_TASKS: set[asyncio.Task] = set()


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


def possible_trigger(schedule: dict, *, now: datetime, attempted_today: bool) -> str | None:
    """Return the one locally due scheduled trigger before its status check."""
    tonight_due_at = _tonight_due_at(schedule, now)
    if tonight_due_at is not None and tonight_due_at <= now < tonight_due_at + SCHEDULE_WINDOW:
        return "tonight"
    if not schedule.get("automatic") or attempted_today:
        return None
    hour, minute = parse_update_schedule_time(schedule["time"])
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return "automatic" if due_at <= now < due_at + SCHEDULE_WINDOW else None


def should_drop_tonight(schedule: dict, *, now: datetime) -> bool:
    """Return whether D7 retires a Tonight choice after its window passes."""
    due_at = _tonight_due_at(schedule, now)
    return due_at is not None and due_at + SCHEDULE_WINDOW <= now


def claim_tonight(schedule: dict, *, db=None) -> bool:
    """Atomically consume the exact Tonight choice read by this tick."""
    expected = schedule.get("tonight_at")
    if expected is None:
        return False
    with write_transaction(db, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if current.get("tonight_at") != expected:
            return False
        current["tonight_at"] = None
        set_setting("update_schedule", current, db=connection)
        return True


def disable_missing_owner_schedule(schedule: dict, *, db=None) -> bool:
    """Clear an exact schedule snapshot when its account no longer exists."""
    with write_transaction(db, scope="update_schedule") as connection:
        current = get_update_schedule(db=connection, include_owner=True)
        if (
            current.get("tonight_at") != schedule.get("tonight_at")
            or current.get("owner_id") != schedule.get("owner_id")
        ):
            return False
        current["tonight_at"] = None
        current["automatic"] = False
        set_setting("update_schedule", current, db=connection)
        return True


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


async def run_scheduled_tick(now: datetime | None = None) -> str | None:
    """Apply D7's scheduled-update transition matrix once.

    An expired Tonight choice clears without a row or user-facing state and sends
    one detached conductor notice. A due, unavailable update simply consumes
    Tonight. A missing owner disables automatic updates, clears Tonight, and
    sends one detached conductor notice. Every other non-due or failed-check
    outcome preserves the schedule.
    """
    from api.system_admin import launch_update
    from automation.update_failure_notice import tell_scheduled_lapse_detached
    from infra.accounts import get_user
    from infra.db_async import run_in_db
    from infra.settings import get_update_schedule
    from infra.update_status import update_status

    local_now = now or datetime.now().astimezone()
    schedule = await asyncio.to_thread(get_update_schedule, include_owner=True)
    if should_drop_tonight(schedule, now=local_now):
        if await run_in_db(claim_tonight, schedule):
            due_at = _tonight_due_at(schedule, local_now)
            window = due_at.strftime("%Y-%m-%d %H:%M %Z") if due_at else schedule["time"]
            tell_scheduled_lapse_detached(
                schedule.get("owner_id"),
                f"Tonight's scheduled update did not run in its window ({window}); the choice was cleared. "
                "Scheduled-update log lines name the cause.",
                delivery_id=f"scheduled-update-lapsed:{schedule.get('tonight_at')}",
                tasks=_SCHEDULE_NOTICE_TASKS,
            )
        return None
    attempted_today = await run_in_db(update_attempted_today, now=local_now)
    trigger = possible_trigger(schedule, now=local_now, attempted_today=attempted_today)
    if trigger is None:
        return None
    try:
        details = (await asyncio.to_thread(update_status, False)).to_dict()
    except Exception:
        logger.exception("scheduled update check failed")
        return None
    available = details.get("available")
    if available is False:
        if trigger == "tonight":
            await run_in_db(claim_tonight, schedule)
        return None
    if available is None:
        return None

    owner_id = schedule.get("owner_id")
    user = await run_in_db(get_user, owner_id) if owner_id else None
    if user is None:
        if await run_in_db(disable_missing_owner_schedule, schedule):
            logger.warning("scheduled update disabled because its owner account is missing")
            tell_scheduled_lapse_detached(
                owner_id,
                "Automatic updates were turned off and Tonight was cleared because the account that set them no longer exists.",
                delivery_id=f"scheduled-update-owner-missing:{schedule.get('tonight_at')}",
                tasks=_SCHEDULE_NOTICE_TASKS,
            )
        return None
    port = os.environ.get("BACKEND_PORT", "")
    if not port.isdigit():
        logger.error("scheduled update cannot launch: BACKEND_PORT is not a port number")
        return None

    def claim(connection: Any) -> bool:
        return trigger != "tonight" or claim_tonight(schedule, db=connection)

    launched = await launch_update(
        user,
        None,
        trigger,
        details.get("target_version"),
        port=int(port),
        claim=claim,
    )
    return launched[0] if launched is not None else None
