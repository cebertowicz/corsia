"""The hourly ``llm_usage`` rollup Measurement reads instead of the raw ledger.

Every Measurement usage figure (Cost, Actual, the token series, the Agents
token column and the Models share) is a sum over ledger rows grouped by a few
columns. Grouping half a million rows on every page load cost seconds per
panel, so the grouping is kept, per UTC hour, in ``llm_usage_hourly``: hours
are summed into weeks (or viewer-local days) at read time, and API-equivalent
dollars are still priced at read time from the token sums.

The rollup is a cache that is never trusted past what it can prove:

* It covers hours before ``built_through`` only, and ``built_through`` never
  passes ``now - GRACE`` (``infra.usage_rollup_schema``). Newer rows, and any
  hour a late insert marked dirty, are grouped from the raw ledger by the same
  SQL, so a read is exact whatever the rollup's state.
* Its rows are tagged with ``definition()``, a hash of the grouping SQL and
  grace rule. A changed grouping makes every read raw until a refresh has
  rebuilt it; a migration that changes a rollup-grain ``llm_usage`` value calls
  the schema-layer invalidator, which clears coverage through the same reset-and-refresh path.
* The plan columns are not in the grain, so attributing a turn's usage to a
  plan node (the ledger's one runtime UPDATE) changes nothing it stores.

Refresh runs only in the background, single flight per event loop, started by
any Measurement request. A step reads and groups its rows on the bulk lane,
outside any transaction, then writes each chunk of at most ``CHUNK_ROWS`` rows
on the main pool in one short ``write_transaction`` that compare-and-sets
``built_through``.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import weakref
from datetime import datetime, timedelta
from typing import Any

from core.measurement_scope import query_params
from infra.database import get_db, write_transaction
from infra.db_async import run_bulk, run_in_db
from infra.usage_rollup_schema import GRACE, grace_limit, hour_key

logger = logging.getLogger(__name__)

CHUNK_ROWS = 5000
# Clearing stale cache rows has no per-hour atomicity requirement, unlike building.
# Keep its write transaction small without reducing productive build batches.
CLEAR_CHUNK_ROWS = 500
_STEP = timedelta(days=1)
_HOUR = timedelta(hours=1)
_HOUR_FORMAT = "%Y-%m-%dT%H"

COLUMNS = (
    "hour", "thread_id", "model", "source", "context_manifest_id", "vendor_account_id",
    "admission", "input_tier", "idle_hours",
    "records", "prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens",
    "total_tokens", "fresh_tokens", "fresh_unknown_records", "cost_usd", "cost_records", "first_at",
)
_GROUP_BY = (
    "GROUP BY hour,thread_id,model,source,context_manifest_id,vendor_account_id,admission,input_tier,idle_hours"
)


@functools.cache
def _grouped_select(minutes: bool = False) -> str:
    """The one grouping both the rollup and the raw tail use, over ``llm_usage u``.

    ``minutes`` keys a raw read by the minute instead of the hour, for a viewer zone whose
    midnight is not on the hour; the rollup itself is always hourly.
    """
    # Imported here: these modules import this one to read the rollup.
    from api.measurement_cost import ADMISSION_SQL, IDLE_HOURS_SQL, INPUT_TIER_SQL
    from api.measurement_token_sql import FRESH_TOKEN_SQL, FRESH_TOKEN_UNKNOWN_SQL

    return (
        f"SELECT substr(u.created_at,1,{16 if minutes else 13}) AS hour,u.thread_id AS thread_id,u.model AS model,"
        "u.source AS source,u.context_manifest_id AS context_manifest_id,"
        "u.vendor_account_id AS vendor_account_id,"
        f"{ADMISSION_SQL} AS admission,{INPUT_TIER_SQL} AS input_tier,{IDLE_HOURS_SQL} AS idle_hours,"
        "COUNT(*) AS records,SUM(u.prompt_tokens) AS prompt_tokens,"
        "SUM(u.completion_tokens) AS completion_tokens,"
        "SUM(u.cache_creation_tokens) AS cache_creation_tokens,"
        "SUM(u.cache_read_tokens) AS cache_read_tokens,SUM(u.total_tokens) AS total_tokens,"
        f"SUM({FRESH_TOKEN_SQL}) AS fresh_tokens,SUM({FRESH_TOKEN_UNKNOWN_SQL}) AS fresh_unknown_records,"
        "SUM(u.cost_usd) AS cost_usd,COUNT(u.cost_usd) AS cost_records,MIN(u.created_at) AS first_at"
    )


@functools.cache
def definition() -> str:
    """Names the grouping that built the rows; any change to it rebuilds the rollup."""
    text = f"{GRACE}|{_grouped_select()}|{_GROUP_BY}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def rollup_through(db) -> str:
    """The exclusive hour the current rollup covers to, or '' when nothing is covered."""
    row = db.execute("SELECT definition,built_through FROM llm_usage_hourly_state WHERE id=1").fetchone()
    return str(row[1]) if row is not None and row[0] == definition() else ""


def _hour_aligned(bound: str) -> bool:
    return len(bound) == 10 or (len(bound) == 13 and bound[10] == "T")


def rollup_bound(db, from_ts: str, to_ts: str) -> str:
    """The ``:rollup_through`` for one window: rollup hours below it lie wholly inside it.

    A start inside an hour reads raw. An end inside an hour stops the rollup at
    that hour, and the raw tail reads the rest up to the exact end.
    """
    if not _hour_aligned(from_ts):
        return ""
    through = rollup_through(db)
    return through if _hour_aligned(to_ts) else min(through, hour_key(to_ts))


def usage_params(db, params: dict[str, Any]) -> dict[str, Any]:
    """``query_params`` plus the rollup bound ``usage_source`` reads.

    A viewer whose periods start between hours reads raw: the rollup is hourly.
    """
    bound = query_params(params)
    grain = bound.get("grain")
    hourly = grain is None or grain.precision == 13
    return {**bound, "rollup_through": rollup_bound(db, str(bound["from_ts"]), str(bound["to_ts"])) if hourly else ""}


def usage_source(scope_threads: str | None, grain=None) -> str:
    """A subquery of hourly usage groups for the scope and window, aliased by the caller.

    Covered clean hours come from the rollup; hours past ``:rollup_through`` and
    dirty hours are grouped from the raw ledger by the same SQL. Rows carry
    ``COLUMNS``; a reader sums ``records`` where it used to count rows.
    ``scope_threads=None`` reads every thread's usage, including rows with none.
    """
    def in_scope(column: str) -> str:
        return f" AND {column} IN (SELECT id FROM {scope_threads})" if scope_threads else ""

    raw = _grouped_select(grain is not None and grain.precision != 13)
    return f"""(
        SELECT {",".join(f"h.{column}" for column in COLUMNS)} FROM llm_usage_hourly h
        WHERE h.hour>=:from_ts AND h.hour<:to_ts AND h.hour<:rollup_through{in_scope("h.thread_id")}
          AND NOT EXISTS (SELECT 1 FROM llm_usage_hourly_dirty d WHERE d.hour=h.hour)
        UNION ALL
        {raw} FROM llm_usage u
        WHERE u.created_at>=:from_ts AND u.created_at<:to_ts AND u.created_at>=:rollup_through
          {in_scope("u.thread_id")}
        {_GROUP_BY}
        UNION ALL
        {raw} FROM llm_usage_hourly_dirty d
        JOIN llm_usage u ON u.created_at>=d.hour AND u.created_at<d.hour||':60'
        WHERE d.hour>=:from_ts AND d.hour<:to_ts AND d.hour<:rollup_through{in_scope("u.thread_id")}
        {_GROUP_BY}
    )"""


# --- Refresh ---------------------------------------------------------------------


def _hour(key: str) -> datetime:
    return datetime.strptime(key, _HOUR_FORMAT)


def _key(moment: datetime) -> str:
    return moment.strftime(_HOUR_FORMAT)


def _grouped(db, start: str, end: str) -> list[tuple]:
    return [tuple(row) for row in db.execute(
        f"{_grouped_select()} FROM llm_usage u WHERE u.created_at>=? AND u.created_at<? {_GROUP_BY} ORDER BY hour",
        (start, end),
    ).fetchall()]


def _chunks(rows: list[tuple], start: str, end: str) -> list[tuple[str, str, list[tuple]]]:
    """Split ``[start, end)`` at hour boundaries into ranges of at most ``CHUNK_ROWS`` rows.

    One hour is never split, so an hour with more groups than that is written alone.
    """
    hours: dict[str, list[tuple]] = {}
    for row in rows:
        hours.setdefault(row[0], []).append(row)
    chunks: list[tuple[str, str, list[tuple]]] = []
    low, batch = start, list[tuple]()
    for hour, hour_rows in hours.items():
        if batch and len(batch) + len(hour_rows) > CHUNK_ROWS:
            chunks.append((low, hour, batch))
            low, batch = hour, []
        batch.extend(hour_rows)
    chunks.append((low, end, batch))
    return chunks


_INSERT = f"INSERT INTO llm_usage_hourly({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"


def _write_range(db, expected: str, low: str, high: str, rows: list[tuple]) -> bool:
    """Replace ``[low, high)`` and move ``built_through`` from ``expected`` to ``high``."""
    with write_transaction(db, scope="usage_rollup_build"):
        current = db.execute("SELECT built_through FROM llm_usage_hourly_state WHERE id=1").fetchone()
        if current is None or current[0] != expected:
            return False
        db.execute("DELETE FROM llm_usage_hourly WHERE hour>=? AND hour<?", (low, high))
        db.executemany(_INSERT, rows)
        db.execute("UPDATE llm_usage_hourly_state SET built_through=? WHERE id=1", (high,))
    return True


def _reset(db) -> bool:
    """Adopt the current definition with nothing covered; later steps clear the old rows."""
    with write_transaction(db, scope="usage_rollup_reset"):
        db.execute(
            "INSERT INTO llm_usage_hourly_state(id,definition,built_through) VALUES (1,?,'') "
            "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,built_through=''",
            (definition(),),
        )
    return True


def _clear(db, stale: list[tuple]) -> bool:
    """Delete one chunk of rows an old definition left, unless a build has started since."""
    with write_transaction(db, scope="usage_rollup_clear"):
        state = db.execute("SELECT definition,built_through FROM llm_usage_hourly_state WHERE id=1").fetchone()
        if state is None or state[0] != definition() or state[1] != "":
            return False
        db.executemany("DELETE FROM llm_usage_hourly WHERE rowid=?", stale)
    return True


def _rebuild_hour(db, hour: str, seq: int, rows: list[tuple]) -> bool:
    """Replace one dirty hour; its mark stays if a late insert bumped it after ``rows`` were read,
    and nothing is written unless the hour is still covered under the definition it was planned with."""
    with write_transaction(db, scope="usage_rollup_dirty"):
        state = db.execute("SELECT definition,built_through FROM llm_usage_hourly_state WHERE id=1").fetchone()
        if state is None or state[0] != definition() or hour >= str(state[1]):
            return False
        db.execute("DELETE FROM llm_usage_hourly WHERE hour=?", (hour,))
        db.executemany(_INSERT, rows)
        db.execute("DELETE FROM llm_usage_hourly_dirty WHERE hour=? AND seq=?", (hour, seq))
    return True


def plan_step(db, now: datetime | None = None) -> tuple[list[functools.partial], bool]:
    """Read one bounded unit of rollup work; return its writes and whether more remains.

    A unit is a reset, one chunk of old rows cleared, one day built forward, or
    one dirty hour rebuilt. Planning only reads, so it runs on the bulk lane.
    Each returned write is one short transaction on a connection that may write,
    and re-checks the state it was planned from.
    """
    state = db.execute("SELECT definition,built_through FROM llm_usage_hourly_state WHERE id=1").fetchone()
    if state is None or state[0] != definition():
        return [functools.partial(_reset)], True
    through, limit = str(state[1]), grace_limit(now)
    if not through:
        stale = db.execute("SELECT rowid FROM llm_usage_hourly LIMIT ?", (CLEAR_CHUNK_ROWS,)).fetchall()
        if stale:
            return [functools.partial(_clear, stale=[tuple(row) for row in stale])], True
        first = db.execute("SELECT MIN(created_at) FROM llm_usage").fetchone()[0]
        start = min(hour_key(first), limit) if first else limit
        return [functools.partial(_write_range, expected="", low=start, high=start, rows=[])], start < limit
    if through < limit:
        end = min(_key(_hour(through) + _STEP), limit)
        writes: list[functools.partial] = []
        expected = through
        for low, high, rows in _chunks(_grouped(db, through, end), through, end):
            writes.append(functools.partial(_write_range, expected=expected, low=low, high=high, rows=rows))
            expected = high
        return writes, True
    dirty = db.execute(
        "SELECT hour,seq FROM llm_usage_hourly_dirty WHERE hour<? ORDER BY hour LIMIT 1", (through,),
    ).fetchone()
    if dirty is None:
        return [], False
    hour, seq = str(dirty[0]), int(dirty[1])
    rows = _grouped(db, hour, _key(_hour(hour) + _HOUR))
    return [functools.partial(_rebuild_hour, hour=hour, seq=seq, rows=rows)], True


def refresh_step(db, now: datetime | None = None) -> bool:
    """Plan and write one unit on one connection; return whether more remains."""
    writes, more = plan_step(db, now)
    return all(write(db) for write in writes) and more


def _plan_job() -> tuple[list[functools.partial], bool]:
    db = get_db()
    try:
        return plan_step(db)
    finally:
        db.close()


def _write_job(write: functools.partial) -> bool:
    db = get_db()
    try:
        return write(db)
    finally:
        db.close()


_refreshing: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task] = weakref.WeakKeyDictionary()


async def _refresh(read, write) -> None:
    try:
        while True:
            writes, more = await read(_plan_job)
            for job in writes:
                if not await write(_write_job, job):
                    return  # the state moved after planning; the next Measurement request restarts
            if not more:
                return
    except Exception:
        logger.exception("usage rollup refresh failed; Measurement reads stay raw past the rollup")


def schedule_refresh(read=run_bulk, write=run_in_db) -> None:
    """Start the background refresh on this loop unless one is already running.

    Each step reads and groups on ``read``, the bulk lane Measurement reads use,
    which may not write the main database; it then writes each chunk on
    ``write``, the main pool, in one short transaction. A Measurement request
    may queue behind one step's read, never behind the whole refresh, and a
    write holds a main-pool worker only for its insert.
    """
    loop = asyncio.get_running_loop()
    running = _refreshing.get(loop)
    if running is not None and not running.done():
        return
    _refreshing[loop] = loop.create_task(_refresh(read, write))


async def refresh_on_request() -> None:
    """A dependency of every Measurement route: make sure the refresh is running."""
    schedule_refresh()
