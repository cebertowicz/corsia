"""Script schedules: registration, claiming and run status for model-free runs.

A script schedule is a row of ``automation_schedules`` with kind ``script``. The
owner registers a file it wrote; the row pins the file's sha256 and carries its
series (the readings the script may emit) in ``payload_json``. This module owns
what is specific to that kind: its bounds, its caps, the run claim and the run
status. Create, cancel, list and retire stay in ``automation.schedules``.

A script schedule never expires: it lives until it is cancelled or its owner is
retired or deleted, so no collector stops quietly.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from automation.schedules import MAX_CADENCE_SECONDS, Schedule, _parse, _row
from host_access.credential_sanitizer import sanitize_credential_text
from infra.database import write_transaction

SCRIPT_KIND = "script"
MIN_CADENCE_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
MAX_PER_AGENT = 10
MAX_PER_INSTANCE = 100
MAX_SERIES = 20
MAX_SCRIPT_BYTES = 1_000_000
# The runs counter keeps counting; a script row is never exhausted.
UNBOUNDED_RUNS = 2**31 - 1
MAX_ERROR_CHARS = 500

#: Run outcomes written to ``last_status``, worst first.
STATUS_ORDER = (
    "timeout", "error", "hash_mismatch", "no_data", "breach", "ok",
)
WORSE_DIRECTIONS = ("up", "down")
REDUCERS = ("last", "mean", "max")
SERIES_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class Series:
    """One reading a script may emit, with the owner's own bounds."""

    key: str
    unit: str
    min: float | None = None
    max: float | None = None
    #: Which direction is worse and how a period's readings reduce to one value.
    #: Stored for the measurement seam; nothing in the runner reads them yet.
    worse: str | None = None
    reduce: str = "last"


def _number(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"series {name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"series {name} must be finite")
    return float(value)


def parse_series(raw: Any) -> list[Series]:
    """Validate the registration's series list."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SERIES:
        raise ValueError(f"series must list 1 to {MAX_SERIES} readings")
    parsed: list[Series] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each series must be an object with a key")
        unknown = set(item) - {"key", "unit", "min", "max", "worse", "reduce"}
        if unknown:
            raise ValueError(f"series has unknown fields: {sorted(unknown)}")
        key = item.get("key")
        if not isinstance(key, str) or not SERIES_KEY_RE.fullmatch(key):
            raise ValueError(
                "series key must be 1-64 lowercase letters, digits, '_', '.' or '-'"
            )
        unit = item.get("unit", "")
        if not isinstance(unit, str) or len(unit) > 32:
            raise ValueError("series unit must be text of at most 32 characters")
        low, high = _number(item.get("min"), "min"), _number(item.get("max"), "max")
        if low is not None and high is not None and low > high:
            raise ValueError("series min must not exceed max")
        worse, reduce = item.get("worse"), item.get("reduce", "last")
        if worse not in (None, *WORSE_DIRECTIONS):
            raise ValueError(f"series worse must be one of {list(WORSE_DIRECTIONS)}")
        if reduce not in REDUCERS:
            raise ValueError(f"series reduce must be one of {list(REDUCERS)}")
        parsed.append(Series(key, unit, low, high, worse, reduce))
    if len({s.key for s in parsed}) != len(parsed):
        raise ValueError("series keys must be unique")
    return parsed


def series_of(schedule: Schedule) -> list[Series]:
    """The series a stored script schedule registered."""
    stored = (schedule.payload or {}).get("series", [])
    return [
        Series(
            str(s["key"]), str(s.get("unit", "")), s.get("min"), s.get("max"),
            s.get("worse"), str(s.get("reduce", "last")),
        )
        for s in stored
    ]


def pin_script(path: str, *, workspace: Path) -> tuple[Path, str]:
    """Resolve the owner's script file and return it with its sha256.

    The file must be a regular file of at most ``MAX_SCRIPT_BYTES`` inside the
    scope the owner can read. A relative path is taken from the workspace.
    """
    from tools.sandbox_config import is_path_allowed

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve(strict=False)
    if not is_path_allowed(candidate, for_write=False):
        raise ValueError("script_path is outside the scope you can read")
    if not resolved.is_file():
        raise ValueError("script_path must be an existing file")
    if resolved.stat().st_size > MAX_SCRIPT_BYTES:
        raise ValueError("script file is larger than 1 MB")
    return resolved, file_sha256(resolved)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_script(
    thread_id: str,
    scope_id: str,
    *,
    script_path: Path,
    script_sha256: str,
    cadence_seconds: int,
    series: list[Series],
    timeout_seconds: int,
    now: str,
    db,
) -> Schedule:
    """Register one script schedule, due at once, under the per-agent and instance caps."""
    if isinstance(cadence_seconds, bool) or not (
        MIN_CADENCE_SECONDS <= cadence_seconds <= MAX_CADENCE_SECONDS
    ):
        raise ValueError(
            f"cadence_seconds must be between {MIN_CADENCE_SECONDS} and "
            f"{MAX_CADENCE_SECONDS}"
        )
    if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    payload = json.dumps(
        {"series": [vars(s) for s in series]}, sort_keys=True,
    )
    schedule_id = uuid.uuid4().hex
    with write_transaction(db, scope="automation_script_create") as connection:
        _require_capacity(connection, thread_id)
        connection.execute(
            """INSERT INTO automation_schedules
               (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,
                payload_json,created_by_actor,created_at,updated_at,
                script_path,script_sha256,timeout_seconds)
               VALUES (?,?,?,'script',?,?,?,'active',?,?,?,?,?,?,?)""",
            (
                schedule_id, thread_id, scope_id, str(cadence_seconds), now,
                UNBOUNDED_RUNS, payload, thread_id, now, now,
                str(script_path), script_sha256, timeout_seconds,
            ),
        )
        row = connection.execute(
            "SELECT * FROM automation_schedules WHERE id=?", (schedule_id,),
        ).fetchone()
        return _row(row)


def _require_capacity(connection, thread_id: str) -> None:
    live = "kind='script' AND state IN ('active','paused')"
    own = connection.execute(
        f"SELECT COUNT(*) FROM automation_schedules WHERE {live} AND thread_id=?",
        (thread_id,),
    ).fetchone()[0]
    if own >= MAX_PER_AGENT:
        raise ValueError(
            f"you already have {MAX_PER_AGENT} script schedules; cancel one first"
        )
    total = connection.execute(
        f"SELECT COUNT(*) FROM automation_schedules WHERE {live}"
    ).fetchone()[0]
    if total >= MAX_PER_INSTANCE:
        raise ValueError(
            f"this instance already has {MAX_PER_INSTANCE} script schedules"
        )


def next_slot(due_at: str, cadence_seconds: int, now: str) -> str:
    """The first cadence slot after ``now``, anchored on the due time: no replay."""
    due, current = _parse(due_at), _parse(now)
    step = timedelta(seconds=cadence_seconds)
    if due > current:
        return due.isoformat()
    missed = int((current - due) / step) + 1
    return (due + missed * step).isoformat()


def claim(schedule: Schedule, now: str, *, db) -> bool:
    """Mark one due run in flight and move the row to its next slot, exactly once."""
    cursor = db.execute(
        """UPDATE automation_schedules
           SET running_since=?, next_run_at=?, updated_at=?
           WHERE id=? AND kind='script' AND state='active'
             AND running_since IS NULL AND next_run_at=?""",
        (
            now,
            next_slot(schedule.next_run_at, int(schedule.spec), now),
            now,
            schedule.id,
            schedule.next_run_at,
        ),
    )
    return cursor.rowcount == 1


def due_scripts(now: str, *, db) -> list[Schedule]:
    """Active script schedules due and not in flight, oldest first."""
    rows = db.execute(
        """SELECT * FROM automation_schedules
           WHERE kind='script' AND state='active' AND running_since IS NULL
             AND next_run_at<=?
           ORDER BY next_run_at,id""",
        (now,),
    ).fetchall()
    return [_row(row) for row in rows]


def in_flight_ids(*, db) -> list[str]:
    """Script schedules whose row says a run is in flight."""
    return [
        str(row[0])
        for row in db.execute(
            "SELECT id FROM automation_schedules "
            "WHERE kind='script' AND running_since IS NOT NULL"
        )
    ]


def finish_run(
    schedule_id: str,
    *,
    ran_at: str,
    status: str,
    error: str | None = None,
    noted: bool = False,
    db,
) -> None:
    """Record one settled run on the schedule row and clear its in-flight mark."""
    if error is not None:
        error = sanitize_credential_text(error)[:MAX_ERROR_CHARS]
    db.execute(
        """UPDATE automation_schedules
           SET runs=runs+1, last_run_at=?, running_since=NULL, last_status=?,
               last_error=?, noted_at=CASE WHEN ? THEN ? ELSE noted_at END,
               updated_at=?
           WHERE id=? AND kind='script'""",
        (ran_at, status, error, int(noted), ran_at, ran_at, schedule_id),
    )


def stop_runs(schedule_id: str, *, status: str, error: str, now: str, db) -> None:
    """Stop a script's runs without cancelling it: its owner must act."""
    db.execute(
        """UPDATE automation_schedules
           SET state='paused', running_since=NULL, last_status=?, last_error=?,
               last_run_at=?, updated_at=?
           WHERE id=? AND kind='script' AND state='active'""",
        (status, error[:MAX_ERROR_CHARS], now, now, schedule_id),
    )


def cancel_for_missing_owner(schedule_id: str, *, now: str, db) -> None:
    """Cancel a schedule whose owner is deleted, archived or retired."""
    db.execute(
        """UPDATE automation_schedules
           SET state='cancelled', running_since=NULL, last_status='owner_gone',
               updated_at=?
           WHERE id=? AND state IN ('active','paused')""",
        (now, schedule_id),
    )


def worst_status(statuses: list[str]) -> str:
    """The most urgent of a run's per-series statuses."""
    for candidate in STATUS_ORDER:
        if candidate in statuses:
            return candidate
    return "ok"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
