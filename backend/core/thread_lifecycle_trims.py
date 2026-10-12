"""The retention sweep's two trims: stale tool payloads and stale ``working/`` files.

Owned by ``core.thread_lifecycle``'s sweep and run from it; split out so the
lifecycle module holds the state machine and its writers.
"""

from __future__ import annotations

import errno
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from core.thread_lifecycle import (
    TOOL_TRIM_MARKER,
    LifecycleConfig,
    RetentionReport,
    SETTLED_WORKER_REASON,
    _iso,
    _row_get,
    _thread_dir,
)
from infra.database import write_transaction

WORKING_KEEP_MARKER = ".corsia-keep"
TOOL_TRIM_BATCH_SIZE = 100
# A batch also ends once it has replaced this many characters of old content
# or held the writer this long, whichever comes first, and always trims at
# least one row.
# The FTS delete re-tokenizes the old content (about 5 ms per MB measured), so
# rows alone do not bound the hold when payloads run to megabytes (#2948).
TOOL_TRIM_BATCH_CHARS = 16 * 1024 * 1024
TOOL_TRIM_BATCH_SECONDS = 0.25
_clock = time.monotonic

# Kept as one predicate so the unlocked candidate read and each writer-side
# recheck cannot drift on which payloads retention may replace.
#
# CROSS JOIN pins SQLite's join order to thread -> checkpoint -> boundary.
# Left to itself the planner walks ``boundary`` by the ``seq >= m.seq`` range
# and scans every installed checkpoint per message, so one row cost
# O(later messages x checkpoints): about 0.55 s on a 20k-message thread with
# 200 checkpoints, which held the writer for 36.6 s per batch (#2948).
_TRIMMABLE_TOOL_PAYLOAD = """
    m.role = 'tool'
    AND m.created_at < ?
    AND m.seq IS NOT NULL
    AND m.content != ''
    AND substr(m.content, 1, ?) != ?
    AND EXISTS (
        SELECT 1
        FROM threads AS s
        CROSS JOIN checkpoints AS p
        CROSS JOIN messages AS boundary
        WHERE s.id = m.thread_id
          AND p.thread_id = s.id
          AND p.history_epoch = s.history_epoch
          AND p.status = 'installed'
          AND p.through_seq >= m.seq
          AND boundary.thread_id = p.thread_id
          AND boundary.seq = p.through_seq
          AND boundary.id = p.through_message_id
          AND NOT EXISTS (
              SELECT 1
              FROM checkpoints AS newer
              WHERE newer.thread_id = p.thread_id
                AND newer.history_epoch = p.history_epoch
                AND newer.status = 'installed'
                AND newer.generation_order > p.generation_order
          )
    )
"""


def _trim_candidates(db, cutoff: str) -> list:
    """Read marker inputs before any SQLite writer lock is acquired."""
    marker_length = len(TOOL_TRIM_MARKER)
    return db.execute(
        "SELECT m.id, length(m.content) AS content_length, "
        "length(m.content) - length(replace(m.content, char(10), '')) + 1 AS lines "
        f"FROM messages AS m WHERE {_TRIMMABLE_TOOL_PAYLOAD}",
        (cutoff, marker_length, TOOL_TRIM_MARKER),
    ).fetchall()


def _trim_tool_payloads(db, now: datetime, cfg: LifecycleConfig,
                        report: RetentionReport) -> None:
    """Trim covered tool payloads in short, revalidated writer batches."""
    cutoff = _iso(now - timedelta(days=cfg.tool_trim_after_days))
    rows = _trim_candidates(db, cutoff)
    first = 0
    while first < len(rows):
        with write_transaction(db, scope="thread_lifecycle_tool_trim"):
            first = _trim_batch(db, rows, first, cutoff, cfg, report)


def _batch_spent(started: float, spent_chars: int) -> bool:
    return (spent_chars >= TOOL_TRIM_BATCH_CHARS
            or _clock() - started >= TOOL_TRIM_BATCH_SECONDS)


def _trim_batch(db, rows: list, first: int, cutoff: str, cfg: LifecycleConfig,
                report: RetentionReport) -> int:
    """Trim ``rows`` from ``first`` inside the caller's writer until a budget is spent.

    Stops at the row, byte or time budget, whichever comes first, after at
    least one row, and returns the index of the first row left untrimmed.
    """
    marker_length = len(TOOL_TRIM_MARKER)
    started = _clock()
    spent_chars = 0
    index = first
    end = min(len(rows), first + TOOL_TRIM_BATCH_SIZE)
    while index < end and (index == first or not _batch_spent(started, spent_chars)):
        row = rows[index]
        content_length = int(_row_get(row, "content_length"))
        lines = int(_row_get(row, "lines"))
        stub = (
            f"{TOOL_TRIM_MARKER} after {cfg.tool_trim_after_days}d retention — "
            f"{content_length} chars, {lines} lines]"
        )
        cursor = db.execute(
            "UPDATE messages AS m SET content = ?, metadata = json_remove("
            "metadata, '$.tool_result_sha256', '$.tool_result_byte_count') "
            "WHERE m.id = ? AND length(m.content) = ? "
            "AND length(m.content) - length(replace(m.content, char(10), '')) + 1 = ? "
            f"AND {_TRIMMABLE_TOOL_PAYLOAD}",
            (
                stub, _row_get(row, "id"), content_length, lines,
                cutoff, marker_length, TOOL_TRIM_MARKER,
            ),
        )
        report.tool_trimmed += cursor.rowcount
        spent_chars += content_length
        index += 1
    return index


def _entry_times(stat_result: os.stat_result) -> float:
    """Newest of when an entry was added or last changed, never its content age.

    ``st_mtime`` alone is the content's age: extracted wheels and copied files
    keep upstream mtimes. ``st_ctime`` (and ``st_birthtime`` where it exists)
    move when the entry is created here, so the newest of the three is no
    earlier than the moment it was added to ``working/``.
    """
    times = [stat_result.st_mtime, stat_result.st_ctime]
    birth = getattr(stat_result, "st_birthtime", None)
    if birth is not None:
        times.append(birth)
    return max(times)


def _unit_is_stale(unit: Path, cutoff: float) -> bool:
    """True only if nothing anywhere in ``unit`` was added or changed since cutoff.

    Judged with ``lstat`` and a walk that never follows links, so a link's
    target never decides its age. A ``WORKING_KEEP_MARKER`` anywhere in the
    unit keeps the whole unit. Raises ``OSError`` rather than guessing; the
    caller keeps the unit and reports the failure.
    """
    if unit.name == WORKING_KEEP_MARKER:
        return False
    unit_stat = unit.lstat()
    if _entry_times(unit_stat) >= cutoff:
        return False
    if not stat.S_ISDIR(unit_stat.st_mode):
        return True
    for dirpath, dirnames, filenames in os.walk(unit, followlinks=False,
                                                onerror=_raise):
        if WORKING_KEEP_MARKER in filenames:
            return False
        for name in (*dirnames, *filenames):
            if _entry_times(os.lstat(os.path.join(dirpath, name))) >= cutoff:
                return False
    return True


def _raise(error: OSError) -> None:
    raise error


def _lstat_or_none(path: Path) -> os.stat_result | None:
    """``lstat``, or None when the entry is missing; any other failure raises."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _remove_unit(unit: Path, cutoff: float) -> bool:
    """Remove a unit judged stale, bottom-up, never following links.

    Each file is re-judged just before it is unlinked and each directory is
    removed only once empty, so an entry added after the unit was judged
    stale survives with every directory above it. True only when the whole
    unit is gone.
    """
    unit_stat = unit.lstat()
    if not stat.S_ISDIR(unit_stat.st_mode):
        return _remove_entry(unit, unit_stat, cutoff)
    whole = True
    for dirpath, dirnames, filenames in os.walk(unit, topdown=False,
                                                followlinks=False, onerror=_raise):
        for name in (*filenames, *dirnames):
            path = Path(dirpath, name)
            path_stat = path.lstat()
            if stat.S_ISDIR(path_stat.st_mode):
                whole = _remove_empty_dir(path) and whole
            else:
                whole = _remove_entry(path, path_stat, cutoff) and whole
    return _remove_empty_dir(unit) and whole


def _remove_entry(path: Path, path_stat: os.stat_result, cutoff: float) -> bool:
    if path.name == WORKING_KEEP_MARKER or _entry_times(path_stat) >= cutoff:
        return False
    path.unlink()
    return True


def _remove_empty_dir(path: Path) -> bool:
    try:
        path.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return False
        raise
    return True


def _trim_working_tmp(db, now: datetime, cfg: LifecycleConfig, workspace: Path,
                      report: RetentionReport) -> None:
    """Trim stale top-level ``working/`` units for eligible threads.

    Active and dormant threads use the normal TTL; only settled temporary
    workers use the shorter one. Other archived threads, ``scratch``, and other
    thread folders remain durable. Keep markers and active turns preserve units.
    Removal re-judges entries before unlinking; filesystem errors keep and log
    the unit, while ``working_files_trimmed`` counts only whole removals.
    """
    from core.turn_core import active_turn_thread_ids
    from registry import service as registry_service

    busy = active_turn_thread_ids(db=db)
    rows = db.execute(
        "SELECT id, project_id, "
        "CASE WHEN COALESCE(lifecycle_state, 'active') = 'archived' THEN 1 ELSE 0 END "
        "AS settled_worker FROM threads WHERE "
        "COALESCE(lifecycle_state, 'active') IN ('active', 'dormant') OR "
        "(COALESCE(lifecycle_state, 'active') = 'archived' "
        "AND archived_reason = ? AND spawned_by IS NOT NULL "
        "AND COALESCE(agent_enabled, 0) = 0)",
        (SETTLED_WORKER_REASON,),
    ).fetchall()
    working_roots: dict[str, Path] = {}
    for r in rows:
        sid = _row_get(r, "id")
        ttl_days = (
            cfg.settled_worker_working_ttl_days
            if _row_get(r, "settled_worker")
            else cfg.working_ttl_days
        )
        cutoff = (now - timedelta(days=ttl_days)).timestamp()
        thread_root = _thread_dir(
            db,
            sid,
            _row_get(r, "project_id"),
            workspace,
        )
        if thread_root is None:
            continue
        working = thread_root / "working"
        working_roots[sid] = working
        if sid in busy:
            continue
        try:
            working_stat = _lstat_or_none(working)
            if working_stat is None or not stat.S_ISDIR(working_stat.st_mode):
                continue
            if _lstat_or_none(working / WORKING_KEEP_MARKER) is not None:
                continue
            units = sorted(working.iterdir())
        except OSError as exc:
            _record_trim_error(report, working, exc)
            continue
        for unit in units:
            try:
                if not _unit_is_stale(unit, cutoff):
                    continue
                if not _remove_unit(unit, cutoff):
                    logger.info("working trim kept {}: part of it was added mid-sweep", unit)
                    continue
            except OSError as exc:
                _record_trim_error(report, unit, exc)
                continue
            report.working_files_trimmed += 1

    if not working_roots:
        return

    registry_service.retire_missing_temporary_under_roots(working_roots, db=db)


def _record_trim_error(report: RetentionReport, path: Path, exc: OSError) -> None:
    report.working_trim_errors += 1
    logger.warning("working trim kept {} after a filesystem error: {}", path, exc)
