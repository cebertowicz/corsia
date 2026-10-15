"""What a finished script run means: its readings, its signals, and where each goes.

A script prints one JSON object per line, ``{"series": "<key>", "value": <number>}``,
for series it registered. The runner reads that stdout as data, never as
instructions, and owns every write. The script holds no credential and no API.

Routing is deliberately small:

- a reading inside its bounds becomes a passive note for its owner, collapsed
  per series, at most once an hour per schedule;
- a breach, a failed run, a timeout, a run with no reading for a series, or a
  script that changed on disk wakes the owner, once per series per local day;
  later ones the same day go to the passive digest instead.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from loguru import logger

from automation import script_schedules as store
from automation.schedules import Schedule, _parse
from automation.script_sandbox import STDOUT_CAP_BYTES, ScriptRun
from host_access.credential_sanitizer import sanitize_credential_text

NOTE_INTERVAL = timedelta(hours=1)
MAX_PROBLEMS = 3
WORKSPACE_SCOPE = "__workspace__"
_WAKE_NEXT = (
    "Run automation_list to see this schedule's last_status and last_error, fix "
    "the script, and read the next run's status there. A script that changed "
    "on disk stays stopped: cancel it with automation_cancel and register the "
    "new file with automation_schedule."
)


@dataclass(frozen=True)
class Reading:
    series: str
    value: float


@dataclass(frozen=True)
class Signal:
    """One condition that wakes the owner."""

    kind: str  # breach | error | timeout | no_data | hash_mismatch
    series: str | None
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunReport:
    status: str
    readings: list[Reading]
    signals: list[Signal]
    error: str | None = None


def parse_readings(
    stdout: bytes, registered: dict[str, store.Series],
) -> tuple[list[Reading], list[str]]:
    """Read stdout as JSON lines; return the valid readings and what was wrong."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return [], ["output is not valid UTF-8"]
    readings: dict[str, Reading] = {}
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        problem = _read_line(line, registered, readings)
        if problem:
            problems.append(f"line {number}: {problem}")
    return list(readings.values()), problems


def _read_line(
    line: str, registered: dict[str, store.Series], readings: dict[str, Reading],
) -> str | None:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return "not a JSON object"
    if not isinstance(item, dict) or set(item) != {"series", "value"}:
        return 'expected exactly {"series": ..., "value": ...}'
    key, value = item["series"], item["value"]
    if not isinstance(key, str) or key not in registered:
        return "series is not registered on this schedule"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{key} value is not a number"
    if not math.isfinite(value):
        return f"{key} value is not finite"
    if key in readings:
        return f"{key} appears more than once"
    readings[key] = Reading(key, float(value))
    return None


def classify(series: store.Series, value: float) -> str:
    """``ok``, ``low`` or ``high`` against the registration's own bounds."""
    if series.min is not None and value < series.min:
        return "low"
    if series.max is not None and value > series.max:
        return "high"
    return "ok"


def _stderr_tail(run: ScriptRun) -> str:
    text = run.stderr.decode("utf-8", errors="replace").strip()
    return sanitize_credential_text(text[-300:]) if text else "no error output"


def _failure(schedule: Schedule, kind: str, message: str) -> RunReport:
    name = _script_name(schedule)
    return RunReport(
        kind, [],
        [Signal(kind, None, f'Script "{name}": {message}', {"message": message})],
        error=message,
    )


def _script_name(schedule: Schedule) -> str:
    return (schedule.script_path or "script").rsplit("/", 1)[-1]


def build_report(schedule: Schedule, run: ScriptRun) -> RunReport:
    """Turn one finished run into a status, its readings and its signals. Pure."""
    if run.timed_out:
        return _failure(
            schedule, "timeout",
            f"did not finish within {schedule.timeout_seconds} s and was stopped",
        )
    if run.over_cap:
        return _failure(
            schedule, "error",
            f"printed more than {STDOUT_CAP_BYTES // 1024} KB; print only readings",
        )
    if run.returncode != 0:
        return _failure(
            schedule, "error",
            f"exited with code {run.returncode}: {_stderr_tail(run)}",
        )
    registered = {s.key: s for s in store.series_of(schedule)}
    readings, problems = parse_readings(run.stdout, registered)
    signals = _signals(schedule, registered, readings)
    status = store.worst_status([s.kind for s in signals] or ["ok"])
    if problems:
        message = "; ".join(problems[:MAX_PROBLEMS])
        signals.append(Signal(
            "error", None,
            f'Script "{_script_name(schedule)}" printed output the runner could '
            f"not read: {message}",
            {"message": message},
        ))
        return RunReport("error", readings, signals, error=message)
    return RunReport(status, readings, signals)


def _signals(
    schedule: Schedule,
    registered: dict[str, store.Series],
    readings: list[Reading],
) -> list[Signal]:
    signals: list[Signal] = []
    seen = {r.series: r for r in readings}
    name = _script_name(schedule)
    for key, series in registered.items():
        reading = seen.get(key)
        if reading is None:
            signals.append(Signal(
                "no_data", key,
                f'Script "{name}" printed no reading for series {key}.',
                {"series": key},
            ))
            continue
        verdict = classify(series, reading.value)
        if verdict != "ok":
            signals.append(_breach(name, series, reading.value, verdict))
    return signals


def _breach(name: str, series: store.Series, value: float, verdict: str) -> Signal:
    bound = series.max if verdict == "high" else series.min
    word = "above its max" if verdict == "high" else "below its min"
    unit = f" {series.unit}" if series.unit else ""
    return Signal(
        "breach", series.key,
        f'Script "{name}": {series.key} = {value:g}{unit} is {word} of {bound:g}{unit}.',
        {"series": series.key, "value": value, "min": series.min,
         "max": series.max, "unit": series.unit, "verdict": verdict},
    )


def local_day(now: str) -> str:
    """The instance's local calendar day, the unit of "one wake per day"."""
    return _parse(now).astimezone().date().isoformat()


def wake_message_id(schedule_id: str, series: str | None, day: str) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"corsia-script:{schedule_id}:{series or '-'}:{day}",
    ))


# TODO(measurement seam, Self-Improvement): do not persist raw readings until
# the outcome-event allowlist and numeric series registry can retain their values.

def _wake_exists(db: sqlite3.Connection, message_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE id=?", (message_id,),
    ).fetchone() is not None


def _note(db: sqlite3.Connection, schedule: Schedule, key: str, summary: str) -> None:
    from infra.passive_changes import append_thread_context_note

    append_thread_context_note(
        db, thread_id=schedule.thread_id, summary=summary,
        details={"path": f"script:{schedule.id}:{key}"},
    )


def _wake(db: sqlite3.Connection, schedule: Schedule, signal: Signal, now: str) -> bool:
    """Wake the owner once for this series today; a repeat becomes a note. True if woken."""
    from events.delivery import enqueue_event_wake

    message_id = wake_message_id(schedule.id, signal.series, local_day(now))
    if _wake_exists(db, message_id):
        _note(db, schedule, signal.series or "run", signal.summary)
        return False
    try:
        enqueue_event_wake(
            schedule.thread_id, f"script.{signal.kind}", signal.summary,
            {**signal.details, "schedule_id": schedule.id, "next": _WAKE_NEXT},
            message_id=message_id,
            chip=f"Script {signal.kind.replace('_', ' ')}: {_script_name(schedule)}",
            db=db,
        )
    except ValueError:
        logger.warning(
            "script schedule {} owner {} unavailable; {} not delivered",
            schedule.id, schedule.thread_id, signal.kind,
        )
        return False
    return True


def _note_due(schedule: Schedule, now: str) -> bool:
    return schedule.noted_at is None or _parse(now) - _parse(schedule.noted_at) >= NOTE_INTERVAL


def _note_readings(
    db: sqlite3.Connection,
    schedule: Schedule,
    report: RunReport,
    registered: dict[str, store.Series],
    now: str,
) -> bool:
    """Note the in-bounds readings, hourly at most; a breach is a signal, not a note."""
    if not _note_due(schedule, now):
        return False
    noted = False
    for reading in report.readings:
        series = registered[reading.series]
        if series.min is None and series.max is None:
            continue  # unbounded: the outcome event is its only record
        if classify(series, reading.value) != "ok":
            continue
        unit = f" {series.unit}" if series.unit else ""
        _note(
            db, schedule, reading.series,
            f"Script {_script_name(schedule)}: {reading.series} = "
            f"{reading.value:g}{unit}",
        )
        noted = True
    return noted


def hash_mismatch_report(schedule: Schedule) -> RunReport:
    """The report for a script file that is no longer the one registered."""
    return _failure(
        schedule, "hash_mismatch",
        "changed on disk since it was registered, so its runs are stopped",
    )


def emit_report(
    db: sqlite3.Connection, schedule: Schedule, report: RunReport, *, now: str,
) -> bool:
    """Write a run's events, notes and wakes and settle it; True if the owner was woken."""
    registered = {s.key: s for s in store.series_of(schedule)}
    # TODO(measurement seam, Self-Improvement): persist readings only once the
    # numeric event contract can retain values rather than valueless rows.
    woken = False
    for signal in report.signals:
        woken = _wake(db, schedule, signal, now) or woken
    noted = _note_readings(db, schedule, report, registered, now)
    store.finish_run(
        schedule.id, ran_at=now, status=report.status, error=report.error,
        noted=noted, db=db,
    )
    return woken
