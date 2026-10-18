"""Presence rows with real focus spans: the visit history and the open-visit table.

Measurement attention reads each visit's focus spans, so the generic pass (random text in a
``focus_spans`` column) cannot stand in for them. Both tables hold the census's row counts. The history also
holds two heavy switchers, each a thread opened on every one of the last twelve weeks' days (84 visits) with
many focus spans a day: the cases the Measurement attention read must stay inside its speed-gate limit for.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from .sqlite_pin import sqlite3
from . import fixture_spec as spec
from .fixture_core import THREAD_COUNT
from .fixture_rows import END, START

#: (thread, focus spans a day) of each heavy switcher.
HEAVY_CASES = (("thread-00000", 100), ("thread-00001", 200))
HEAVY_DAYS = 84
EXIT_REASONS = ("navigated", "hidden", "idle", "disconnected")
#: How many of the open-visit rows are exit-pending (still present, exit detected).
PENDING_ROWS = 6


def _stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _spans(rng: random.Random, entered: datetime, exited: datetime) -> list[list[str]]:
    """One to six ordered, non-overlapping spans inside the visit."""
    seconds = int((exited - entered).total_seconds())
    cuts = sorted(rng.sample(range(seconds + 1), 2 * rng.randint(1, 6)))
    return [[_stamp(entered + timedelta(seconds=a)), _stamp(entered + timedelta(seconds=b))]
            for a, b in zip(cuts[::2], cuts[1::2]) if a < b]


def _heavy_visit(day: datetime, per_day: int) -> tuple[str, str, list[list[str]]]:
    """A 09:00 to 18:00 visit holding ``per_day`` equal spans, each focused for 55% of its stride."""
    entered = day.replace(hour=9, minute=0, second=0, microsecond=0)
    stride = 9 * 3600 // per_day
    spans = [[_stamp(entered + timedelta(seconds=n * stride)),
              _stamp(entered + timedelta(seconds=n * stride + stride * 55 // 100))] for n in range(per_day)]
    return _stamp(entered), _stamp(entered + timedelta(hours=9)), spans


def _projects(db: sqlite3.Connection) -> dict[str, str | None]:
    return {row[0]: row[1] for row in db.execute("SELECT id, project_id FROM threads")}


def seed_visits(db: sqlite3.Connection, rng: random.Random) -> int:
    """The finished-visit history: the heavy switchers, then visits with one to six focus spans."""
    projects = _projects(db)
    horizon = int((END - START).total_seconds()) - 2 * 3600
    rows = []
    for thread, per_day in HEAVY_CASES:
        for back in range(1, HEAVY_DAYS + 1):
            entered, exited, spans = _heavy_visit(END - timedelta(days=back), per_day)
            rows.append(("steve", "chat", thread, projects[thread], entered, exited, 9 * 3600, 1, "navigated",
                         json.dumps(spans, separators=(",", ":"))))
    for _ in range(spec.target_rows("presence_visits") - len(rows)):
        thread = f"thread-{rng.randrange(THREAD_COUNT):05d}"
        entered = START + timedelta(seconds=rng.randrange(horizon))
        exited = entered + timedelta(minutes=rng.randint(1, 90))
        rows.append(("steve", "chat", thread, projects[thread], _stamp(entered), _stamp(exited),
                     int((exited - entered).total_seconds()), rng.randint(1, 2), rng.choice(EXIT_REASONS),
                     json.dumps(_spans(rng, entered, exited), separators=(",", ":"))))
    db.executemany(
        "INSERT INTO presence_visits (user_id,surface,thread_id,project_id,entered_at,exited_at,seconds,"
        "sessions,exit_reason,focus_spans) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def seed_open_visits(db: sqlite3.Connection, rng: random.Random) -> int:
    """One row per thread: a few exit-pending visits that still hold spans, the rest finalized (spans cleared).

    An open visit is minutes old on a running instance (the sweep finalizes it within the merge window), so
    the pending ones entered in the last two hours; a finalized row's last visit may be any age.
    """
    want = spec.target_rows("thread_presence")
    threads = rng.sample(range(THREAD_COUNT), want)
    rows = []
    for index, number in enumerate(threads):
        pending = index < PENDING_ROWS
        entered = (END - timedelta(hours=2) + timedelta(seconds=rng.randrange(3600)) if pending
                   else START + timedelta(seconds=rng.randrange(int((END - START).total_seconds()) - 7200)))
        exited = entered + timedelta(minutes=rng.randint(5, 60))
        rows.append((
            "steve", f"thread-{number:05d}", int(pending), _stamp(entered), None if pending else _stamp(exited),
            _stamp(exited) if pending else None, _stamp(exited) if pending else None,
            "hidden" if pending else None, _stamp(exited), _stamp(exited),
            json.dumps(_spans(rng, entered, exited), separators=(",", ":")) if pending else None))
    db.executemany(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,last_exited_at,"
        "exit_pending_at,exit_pending_since,exit_pending_reason,last_confirmed_at,updated_at,focus_spans)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)
