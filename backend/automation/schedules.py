"""Actor-bound bounded clock and cadence schedule store."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from infra.database import write_transaction

MAX_SCHEDULE_RUNS = 1000
MAX_CADENCE_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class Schedule:
    id: str
    thread_id: str
    scope_id: str
    kind: str
    spec: str
    next_run_at: str
    last_run_at: str | None
    runs: int
    max_runs: int
    state: str
    payload: dict | None
    created_by_actor: str
    asked_by_user: int
    created_at: str
    updated_at: str


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row(row) -> Schedule:
    values = dict(row)
    payload_json = values.pop("payload_json")
    values["payload"] = json.loads(payload_json) if payload_json else None
    return Schedule(**values)


def _next_run(kind: str, spec: str, now: str, max_runs: int) -> str:
    if isinstance(max_runs, bool) or not 1 <= max_runs <= MAX_SCHEDULE_RUNS:
        raise ValueError(f"max_runs must be between 1 and {MAX_SCHEDULE_RUNS}")
    now_dt = _parse(now)
    if kind == "clock":
        if max_runs != 1:
            raise ValueError("clock schedules require max_runs=1")
        due = _parse(spec)
        if due <= now_dt:
            raise ValueError("clock schedule must be in the future")
        return due.isoformat()
    if kind != "cadence":
        raise ValueError("schedule kind must be clock or cadence")
    try:
        seconds = int(spec)
    except (TypeError, ValueError) as exc:
        raise ValueError("cadence spec must be integer seconds") from exc
    if not 1 <= seconds <= MAX_CADENCE_SECONDS:
        raise ValueError(
            f"cadence seconds must be between 1 and {MAX_CADENCE_SECONDS}"
        )
    return (now_dt + timedelta(seconds=seconds)).isoformat()


def create(
    thread_id: str,
    scope_id: str,
    actor_id: str,
    *,
    kind: str,
    spec: str,
    max_runs: int,
    payload: dict | None = None,
    asked_by_user: bool = False,
    now: str,
    db,
) -> Schedule:
    """Create one mandatory-bounded schedule for the current actor."""

    next_run_at = _next_run(kind, spec, now, max_runs)
    schedule_id = uuid.uuid4().hex
    payload_json = json.dumps(payload, sort_keys=True) if payload else None
    with write_transaction(db, scope="automation_schedule_create") as connection:
        connection.execute(
            """INSERT INTO automation_schedules
               (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,
                payload_json,created_by_actor,asked_by_user,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'active',?,?,?,?,?)""",
            (
                schedule_id,
                thread_id,
                scope_id,
                kind,
                str(spec),
                next_run_at,
                max_runs,
                payload_json,
                actor_id,
                int(asked_by_user),
                now,
                now,
            ),
        )
        return get(schedule_id, db=connection)


def get(schedule_id: str, *, db) -> Schedule:
    row = db.execute(
        "SELECT * FROM automation_schedules WHERE id=?",
        (schedule_id,),
    ).fetchone()
    if row is None:
        raise KeyError(schedule_id)
    return _row(row)


def list_for_actor(*, thread_id: str, actor_id: str, db) -> list[Schedule]:
    """List only schedules owned by the executing actor and thread."""

    rows = db.execute(
        """SELECT * FROM automation_schedules
           WHERE thread_id=? AND created_by_actor=?
           ORDER BY created_at,id""",
        (thread_id, actor_id),
    ).fetchall()
    return [_row(row) for row in rows]


def cancel(schedule_id: str, *, thread_id: str, actor_id: str, db) -> bool:
    """Cancel an owned active or paused schedule without existence disclosure."""

    now = datetime.now(timezone.utc).isoformat()
    with write_transaction(db, scope="automation_schedule_cancel") as connection:
        cursor = connection.execute(
            """UPDATE automation_schedules
               SET state='cancelled',updated_at=?
               WHERE id=? AND thread_id=? AND created_by_actor=?
                 AND state IN ('active','paused')""",
            (now, schedule_id, thread_id, actor_id),
        )
        return cursor.rowcount == 1


def due(now: str, *, db) -> list[Schedule]:
    """Return active schedules due at the current runner clock."""

    rows = db.execute(
        """SELECT * FROM automation_schedules
           WHERE state='active' AND next_run_at<=?
           ORDER BY next_run_at,id""",
        (now,),
    ).fetchall()
    return [_row(row) for row in rows]


def retire_for_thread(thread_id: str, settled_at: str, *, db) -> int:
    """Cancel a retired thread's open schedules inside the caller's write."""

    cursor = db.execute(
        """UPDATE automation_schedules
           SET state='cancelled',updated_at=?
           WHERE thread_id=? AND state IN ('active','paused')""",
        (settled_at, thread_id),
    )
    return cursor.rowcount


def settle_run(schedule_id: str, fired_at: str, *, db) -> bool:
    """Advance one settled scheduled occurrence exactly once."""

    schedule = get(schedule_id, db=db)
    if schedule.state != "active" or schedule.next_run_at > fired_at:
        return False
    runs = schedule.runs + 1
    exhausted = runs >= schedule.max_runs or schedule.kind == "clock"
    next_run = schedule.next_run_at
    if not exhausted:
        next_run = (
            _parse(schedule.next_run_at)
            + timedelta(seconds=int(schedule.spec))
        ).isoformat()
    cursor = db.execute(
        """UPDATE automation_schedules
           SET runs=?,last_run_at=?,next_run_at=?,state=?,updated_at=?
           WHERE id=? AND state='active' AND runs=? AND next_run_at=?""",
        (
            runs,
            schedule.next_run_at,
            next_run,
            "exhausted" if exhausted else "active",
            fired_at,
            schedule.id,
            schedule.runs,
            schedule.next_run_at,
        ),
    )
    return cursor.rowcount == 1
