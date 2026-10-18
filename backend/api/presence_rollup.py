"""The focus-attention rollup: what Measurement reads instead of every focused span.

A user who switches windows a hundred times a day leaves a hundred focus spans a
day on their visit rows, and the interval union over them costs time quadratic in
their number. The union only depends on which threads were focused when, so this
module keeps that, per 15-minute UTC bucket and per set of threads focused at
once (``infra.presence_rollup_schema``): a read sums buckets, one row per bucket
and set, however often the user switched inside them.

The rollup is a cache that is never trusted past what it can prove:

* It covers buckets before ``built_through`` only. That never passes ``now - GRACE``
  or the start of a visit still open, so a visit that finishes later lands in the
  raw tail Measurement reads from the spans. A row written with an earlier start
  moves ``built_through`` back (a trigger, in the writer's transaction) and the
  refresh rebuilds forward, so a read is exact whatever the rollup's state.
* It is built from the visit rows only; an open visit is never in it.
* A viewer whose periods do not start on a bucket reads raw (``rollup_binds``).

The refresh reads and groups on the bulk lane outside any transaction, then
writes each step in one short ``write_transaction`` that compare-and-sets
``built_through``. ``api.usage_rollup`` runs it, beside the usage refresh.
"""

from __future__ import annotations

import functools
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from infra.database import write_transaction
from infra.presence_rollup_schema import BUCKET_MINUTES, DEFINITION, GRACE
from infra.presence_time import parse
from infra.usage_rollup_schema import utc_now

_EPOCH = datetime(1970, 1, 1)
_BUCKET = BUCKET_MINUTES * 60
#: A catch-up step builds this long a stretch: one pass over the visit rows, one short write.
_STEP = timedelta(days=7)
_STAMP = "%Y-%m-%d %H:%M:%S"

Member = tuple[str, str]  # (thread_id, project_id), '' for none
Totals = dict[tuple[str, str, str], float]  # (user_id, bucket, signature) -> seconds

_VISITS = """
    SELECT COALESCE(t.user_id,v.user_id) AS user_id,v.thread_id,v.project_id,
      datetime(v.entered_at) AS entered,datetime(v.exited_at) AS exited,v.focus_spans
    FROM presence_visits v LEFT JOIN threads t ON t.id=v.thread_id
    WHERE v.entered_at<:hi AND v.exited_at>:lo
      AND ((v.thread_id IS NOT NULL AND t.id IS NOT NULL) OR (v.thread_id IS NULL AND v.project_id IS NOT NULL))
"""


def _stamp(moment: datetime) -> str:
    return moment.strftime(_STAMP)


def _seconds(moment: datetime) -> int:
    return int((moment - _EPOCH).total_seconds())


def _moment(seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def _floor(moment: datetime) -> datetime:
    return _moment(_seconds(moment) // _BUCKET * _BUCKET)


def canonical(text: str) -> datetime | None:
    """A stored or bound timestamp (date, hour, minute or full, ``T`` or space form) as naive UTC."""
    value = text.replace("T", " ")
    try:
        return parse(value + {10: " 00:00:00", 13: ":00:00", 16: ":00"}.get(len(value), "")).replace(microsecond=0)
    except ValueError:
        return None


def _stored(text: str) -> datetime:
    """A timestamp SQLite's ``datetime()`` produced from a stored row, which is never unreadable."""
    moment = canonical(text)
    if moment is None:
        raise ValueError(f"unreadable presence timestamp {text!r}")
    return moment


def _truncated(text: str) -> datetime:
    """A stored span instant as SQLite's ``datetime()`` reads it: whole seconds, naive UTC."""
    return parse(text).replace(microsecond=0)


def _visit_spans(rows, start: int, end: int):
    """Each visit's focused spans clipped to ``[start, end)``: (user, member, began, ended) in epoch seconds."""
    for row in rows:
        entered, exited = _seconds(_stored(row["entered"])), _seconds(_stored(row["exited"]))
        began, ended = max(entered, start), min(exited, end)
        member: Member = (row["thread_id"] or "", row["project_id"] or "")
        if row["focus_spans"] is None:  # predates focus tracking: the whole visit counts
            pairs = [(began, ended)]
        else:
            pairs = [(max(_seconds(_truncated(a)), began), min(_seconds(_truncated(b)), ended))
                     for a, b in json.loads(row["focus_spans"])]
        for low, high in pairs:
            if low < high:
                yield row["user_id"], member, low, high


def _add(totals: Totals, user: str, signature: str, low: int, high: int) -> None:
    """Add ``[low, high)`` to the buckets it covers, one row per bucket the stretch touches."""
    while low < high:
        bucket = low // _BUCKET * _BUCKET
        stop = min(bucket + _BUCKET, high)
        totals[(user, _stamp(_moment(bucket)), signature)] += stop - low
        low = stop


def _sweep(spans: list[tuple[Member, int, int]], user: str, totals: Totals) -> None:
    """One user's union timeline: seconds per bucket for each distinct set of members focused at once."""
    events = sorted((instant, delta, member) for member, low, high in spans
                    for instant, delta in ((low, 1), (high, -1)))
    active: Counter[Member] = Counter()
    signature, previous = "", 0
    for instant, delta, member in events:
        if active and instant > previous:
            _add(totals, user, signature, previous, instant)
        active[member] += delta
        if not active[member]:
            del active[member]
        signature = json.dumps(sorted(active), separators=(",", ":"))
        previous = instant


def slices(db, start: datetime, end: datetime) -> Totals:
    """Seconds focused in ``[start, end)``, per user, bucket and set of members focused at once.

    The one definition of the union the raw SQL in ``api.measurement_presence`` also computes: a visit
    without spans counts whole, every span is clipped to its visit, and instants are whole seconds.
    """
    lo, hi = (start - timedelta(days=1)).date().isoformat(), (end + timedelta(days=1)).date().isoformat()
    by_user: dict[str, list[tuple[Member, int, int]]] = defaultdict(list)
    for user, member, low, high in _visit_spans(db.execute(_VISITS, {"lo": lo, "hi": hi}), _seconds(start), _seconds(end)):
        by_user[user].append((member, low, high))
    totals: Totals = defaultdict(float)
    for user, spans in by_user.items():
        _sweep(spans, user, totals)
    return totals


# --- Refresh ---------------------------------------------------------------------


def build_limit(db, now: datetime | None = None) -> datetime:
    """The first bucket the rollup may not cover yet: ``now - GRACE``, or the start of the oldest open visit."""
    limit = (now or utc_now()) - GRACE
    opened = db.execute("SELECT MIN(datetime(last_entered_at)) FROM thread_presence WHERE present=1").fetchone()[0]
    return _floor(min(limit, canonical(opened)) if opened else limit)


def _reset(db) -> bool:
    """Adopt the current definition with nothing covered."""
    with write_transaction(db, scope="presence_rollup_reset"):
        db.execute(
            "INSERT INTO presence_focus_rollup_state(id,definition,built_through) VALUES (1,?,'') "
            "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,built_through=''",
            (DEFINITION,),
        )
    return True


def _seed(db, start: str) -> bool:
    """Start covering from ``start``, dropping whatever an earlier definition left."""
    with write_transaction(db, scope="presence_rollup_seed"):
        current = db.execute("SELECT built_through FROM presence_focus_rollup_state WHERE id=1").fetchone()
        if current is None or current[0] != "":
            return False
        db.execute("DELETE FROM presence_focus_rollup")
        db.execute("UPDATE presence_focus_rollup_state SET built_through=? WHERE id=1", (start,))
    return True


def _set_ids(db, signatures: list[str]) -> dict[str, int]:
    """The id of each set of members, adding the ones not stored yet.

    Ids are handed out in the order given, so callers pass them sorted: the same history builds the same
    rows on every process (a Python set iterates in a per-process order), and read cost depends on them.
    """
    ids: dict[str, int] = {}
    for signature in signatures:
        found = db.execute("SELECT id FROM presence_focus_sets WHERE signature=?", (signature,)).fetchone()
        if found is None:
            found = (db.execute("INSERT INTO presence_focus_sets(signature) VALUES (?)", (signature,)).lastrowid,)
            db.executemany(
                "INSERT INTO presence_focus_set_members(set_id,thread_id,project_id) VALUES (?,?,?)",
                [(found[0], thread or None, project or None) for thread, project in json.loads(signature)],
            )
        ids[signature] = int(found[0])
    return ids


def _write_range(db, expected: str, low: str, high: str, totals: Totals) -> bool:
    """Replace buckets ``[low, high)`` and move ``built_through`` from ``expected`` to ``high``."""
    with write_transaction(db, scope="presence_rollup_build"):
        current = db.execute("SELECT built_through FROM presence_focus_rollup_state WHERE id=1").fetchone()
        if current is None or current[0] != expected:
            return False
        ids = _set_ids(db, sorted({signature for _, _, signature in totals}))
        db.execute("DELETE FROM presence_focus_rollup WHERE bucket>=? AND bucket<?", (low, high))
        db.executemany(
            "INSERT INTO presence_focus_rollup(user_id,bucket,set_id,seconds) VALUES (?,?,?,?)",
            [(user, bucket, ids[signature], seconds) for (user, bucket, signature), seconds in sorted(totals.items())],
        )
        db.execute("UPDATE presence_focus_rollup_state SET built_through=? WHERE id=1", (high,))
    return True


def plan_step(db, now: datetime | None = None) -> tuple[list[functools.partial], bool]:
    """Read one bounded unit of rollup work; return its writes and whether more remains.

    A unit is a reset, the seed of a rollup with nothing covered, or one stretch built forward. Planning
    only reads, so it runs on the bulk lane; each write is one short transaction that re-checks the state
    it was planned from.
    """
    state = db.execute("SELECT definition,built_through FROM presence_focus_rollup_state WHERE id=1").fetchone()
    if state is None or state[0] != DEFINITION:
        return [functools.partial(_reset)], True
    through, limit = str(state[1]), build_limit(db, now)
    if not through:
        first = db.execute("SELECT MIN(datetime(entered_at)) FROM presence_visits").fetchone()[0]
        start = min(_floor(_stored(first)), limit) if first else limit
        return [functools.partial(_seed, start=_stamp(start))], start < limit
    begin = canonical(through)
    if begin is None or begin >= limit:
        return [], False
    end = min(begin + _STEP, limit)
    write = functools.partial(
        _write_range, expected=through, low=through, high=_stamp(end), totals=slices(db, begin, end),
    )
    return [write], end < limit


def refresh_step(db, now: datetime | None = None) -> bool:
    """Plan and write one unit on one connection; return whether more remains."""
    writes, more = plan_step(db, now)
    return all(write(db) for write in writes) and more


# --- Read bounds -----------------------------------------------------------------


def _on_bucket(moment: datetime | None) -> bool:
    return moment is not None and moment.second == 0 and moment.minute % BUCKET_MINUTES == 0


def rollup_binds(db, bound: dict[str, Any]) -> dict[str, str]:
    """The binds a read of ``bound``'s window needs to use the rollup, or ``{}`` when it must read raw.

    The rollup serves ``[focus_rollup_from, focus_rollup_through)``; the raw spans serve the tail from
    ``focus_rollup_through``. A window whose periods do not start on a bucket reads raw.
    """
    grain = bound.get("grain")
    state = db.execute("SELECT definition,built_through FROM presence_focus_rollup_state WHERE id=1").fetchone()
    if grain is None or state is None or state[0] != DEFINITION or not state[1]:
        return {}
    start, end, built = canonical(str(bound["from_ts"])), canonical(str(bound["to_ts"])), canonical(str(state[1]))
    if not all(_on_bucket(canonical(edge)) for edge in (*grain.starts, grain.end)) or not _on_bucket(start):
        return {}
    if start is None or end is None or built is None:
        return {}
    through = min(built, _floor(end))
    return {"focus_rollup_from": _stamp(start), "focus_rollup_through": _stamp(through)} if through > start else {}
