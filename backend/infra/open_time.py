"""Open time: how long a run, or a plan step, has had something happening in it.

One server number for every surface that shows how long running work has gone.
It is the union of the lead thread's own Turn intervals, every descendant's
Turn intervals (the ``spawned_by`` chain, any depth) and the outside-wait
intervals Plans reports, clipped to the range asked about. Overlaps count
once; idle gaps count nothing. Everything is read from storage, so a reload
answers the same as the live page did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

Interval = tuple[float, float]

_DESCENDANTS_SQL = """
    WITH RECURSIVE family(id, path) AS (
        SELECT id, json_array(id) FROM threads WHERE id = ?
        UNION ALL
        SELECT child.id, json_insert(family.path, '$[#]', child.id)
        FROM threads AS child
        JOIN family ON child.spawned_by = family.id
        WHERE NOT EXISTS (SELECT 1 FROM json_each(family.path) WHERE value = child.id)
    )
    SELECT DISTINCT id FROM family
"""


def epoch(value: str | datetime) -> float:
    """One stored or API UTC timestamp (naive means UTC) as epoch seconds."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def api_time(value: datetime) -> str:
    """RFC 3339 UTC, the form every other API timestamp has."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def thread_and_descendants(db: Any, thread_id: str) -> list[str]:
    """The thread and every thread beneath it by ``spawned_by``, cycle-safe."""
    return [str(row[0]) for row in db.execute(_DESCENDANTS_SQL, (thread_id,))]


def turn_intervals(db: Any, thread_ids: list[str], *, since: float, until: float) -> list[Interval]:
    """Every stored Turn of ``thread_ids`` that touches [since, until]; a running Turn ends at ``until``.

    The range is filtered here, not in SQL: ``started_at`` is stored both naive and offset-aware, so a text
    comparison in SQL is not reliable across rows.
    """
    if not thread_ids:
        return []
    marks = ",".join("?" for _ in thread_ids)
    rows = db.execute(
        f"SELECT started_at, ended_at FROM active_turns WHERE thread_id IN ({marks})",
        thread_ids,
    ).fetchall()
    found = []
    for row in rows:
        start = epoch(row["started_at"])
        end = epoch(row["ended_at"]) if row["ended_at"] else until
        if end > since and start < until:
            found.append((start, end))
    return found


def union(intervals: list[Interval], *, since: float, until: float) -> list[Interval]:
    """Overlap-free intervals, each clipped to [since, until], in order."""
    merged: list[Interval] = []
    for start, end in sorted((max(a, since), min(b, until)) for a, b in intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def open_times(
    db: Any,
    thread_id: str,
    ranges: list[tuple[datetime, datetime | None]],
    *,
    as_of: datetime,
) -> list[dict]:
    """``{seconds, open_now, as_of}`` for each range of one lead thread, in order.

    The family, its Turns and its waits are read once over the widest range asked, then clipped per range.
    """
    from plans import public

    now = epoch(as_of)
    bounds = [(epoch(since), now if until is None else min(epoch(until), now)) for since, until in ranges]
    live = [(low, high) for low, high in bounds if high > low]
    answers: list[dict] = []
    if live:
        wide_low = min(low for low, _ in live)
        wide_high = max(high for _, high in live)
        family = thread_and_descendants(db, thread_id)
        waits = public.open_wait_intervals(
            family,
            since=datetime.fromtimestamp(wide_low, UTC),
            until=datetime.fromtimestamp(wide_high, UTC),
            db=db,
        )
        intervals = turn_intervals(db, family, since=wide_low, until=wide_high)
        intervals += [pair for pairs in waits.values() for pair in pairs]
    for low, high in bounds:
        merged = union(intervals, since=low, until=high) if high > low else []
        answers.append({
            "seconds": sum(end - start for start, end in merged),
            # Something is running or waited on right now only for a range that reaches now.
            "open_now": high >= now and bool(merged) and merged[-1][1] >= now,
            "as_of": api_time(as_of),
        })
    return answers


def open_time(
    db: Any,
    thread_id: str,
    *,
    since: datetime,
    until: datetime | None,
    as_of: datetime,
) -> dict:
    """``{seconds, open_now, as_of}`` for one lead thread over [since, until or now]."""
    return open_times(db, thread_id, [(since, until)], as_of=as_of)[0]
