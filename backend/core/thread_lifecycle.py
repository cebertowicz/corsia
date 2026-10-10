"""Phase 3b — thread lifecycle transitions + retention.

State machine (thresholds are tunable operating config; see ``LifecycleConfig``)::

    active --14d idle--> dormant --30d dormant--> archived --180d--> purged

Transitions are **NOT one-way**: fresh activity on a thread the engine aged into
dormant / dormancy-archived reactivates it to ``active`` (see ``_evaluate_thread``)
so a resumed thread is never silently carried toward purge.

Physical effects are idempotent: completed cleanup is a no-op on the next sweep.

- **archive** — preserve working files, artifacts, ``context/`` and ``skills/``.
- **purge** — commit permanent unavailability, remove raw message history, user presence
  and update records, archive owned temporary artifacts; then delete ``.threads/{id}/``
  outside the database writer (failed cleanup stays purged; this sweep retries).
  Missing owned temporary files become tombstones.
- **tool-I/O trim** — after 30d, replace verbose ``role='tool'`` payloads only
  when the current installed checkpoint covers them. This applies across ALL
  threads (agent/roster included); user/assistant text is untouched.
- **working/ tmp trim** — remove each ``.threads/{id}/working/`` entry whole once
  nothing in it was added for 7d (see thread_lifecycle_trims), tombstone owned
  temp identities. Archived threads retain their files until permanent purge.

The retention *state machine* excludes agent-enabled threads (the roster: conductor,
design agents, …) — an idle agent must not be archived out of the live roster — and
never purges pinned threads. The tool-I/O + working/ trims are global, but tool payloads
become disposable only once an installed checkpoint covers them.

Runs on the nightly schedule; because every transition is guarded by the row's
own state + timestamp and every effect is idempotent, re-running is safe.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from infra.checkpoint_entry_index import delete_thread_checkpoints
from infra.continuity_store import delete_thread_continuity
from infra.presence import delete_thread_presence
from infra.thread_updates import delete_thread_updates
from infra.database import TransactionOwnershipError, write_transaction
from infra.constants import (
    LIFECYCLE_ARCHIVE_AFTER_DAYS,
    LIFECYCLE_DORMANT_AFTER_DAYS,
    LIFECYCLE_PURGE_AFTER_DAYS,
    LIFECYCLE_TOOL_TRIM_AFTER_DAYS,
    LIFECYCLE_WORKING_TTL_DAYS,
)
from thread.runtime_boundary import bump_history_epoch
from infra.thread_driver import delete_thread_turns
from infra.thread_inputs import invalidate_markers_for_archive
from core.thread_lifecycle_plans import (
    announce_lifecycle as _announce_lifecycle,
    wake_parent_of_ended_child as _wake_parent_of_ended_child,
    withdraw_owned_decisions as _withdraw_owned_decisions,
)

# Marker prefix on a trimmed tool payload — makes the trim idempotent (already
# trimmed rows are skipped on re-run).
TOOL_TRIM_MARKER = "[tool output trimmed"


class ThreadLifecycleError(ValueError):
    """A requested lifecycle transition cannot be applied."""


@dataclass(frozen=True)
class LifecycleConfig:
    """Tunable retention thresholds (days). Tests pass tiny values."""

    dormant_after_days: int = LIFECYCLE_DORMANT_AFTER_DAYS
    archive_after_days: int = LIFECYCLE_ARCHIVE_AFTER_DAYS
    purge_after_days: int = LIFECYCLE_PURGE_AFTER_DAYS
    tool_trim_after_days: int = LIFECYCLE_TOOL_TRIM_AFTER_DAYS
    working_ttl_days: int = LIFECYCLE_WORKING_TTL_DAYS


@dataclass
class RetentionReport:
    reactivated: list[str] = field(default_factory=list)
    dormant: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)
    tool_trimmed: int = 0
    working_files_trimmed: int = 0
    working_trim_errors: int = 0
    purge_cleanup_errors: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    """Parse a stored ISO timestamp to an aware UTC datetime (or ``None``)."""
    if not value:
        return None
    ts = str(value)
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _row_get(row, key, default=None):
    """Column access that works for sqlite3.Row and plain tuples-as-dicts."""
    try:
        if hasattr(row, "keys"):
            return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default
    return default


def _thread_dir(db, sid: str, project_id: str | None, workspace: Path) -> Path | None:
    """Resolve ``.threads/{id}`` through the public Workspace layout owner."""
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_root,
        resolve_workspace_root,
    )

    scope = (
        resolve_project_root(workspace, project_id, db=db)
        if project_id
        else resolve_workspace_root(workspace)
    )
    return resolve_thread_root(scope, sid) if scope is not None else None


# --- physical effects -------------------------------------------------------

def _remove_tree(path: Path) -> None:
    """Remove a purged tree or report the retained bytes without reopening it."""
    if not path.exists() and not path.is_symlink():
        return
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass  # Another remover reaching the same outcome is still success.
    if path.exists() or path.is_symlink():
        raise OSError(f"failed to remove lifecycle path: {path}")


# --- transitions ------------------------------------------------------------

def _set_dormant(db, sid: str, now: datetime) -> None:
    # Records the dormancy transition only. Idle is computed from message
    # activity (MAX(created_at)) under the candidate write lock, NOT from
    # updated_at, so this deliberately does not bump updated_at.
    db.execute(
        "UPDATE threads SET lifecycle_state = 'dormant', dormant_at = ? WHERE id = ?",
        (_iso(now), sid),
    )


def _rehome_on_end(db, thread_id: str, reason: str) -> None:
    from registry.ownership import rehome_on_thread_end

    rehome_on_thread_end(db, thread_id, reason)


def set_user_archive_state(
    db,
    sid: str,
    *,
    archived: bool,
    now: datetime | str | None = None,
) -> bool:
    """Synchronously apply the user-facing archive/unarchive transition.

    This changes durable lifecycle state only. The nightly retention sweep owns
    physical purge, so reversible archiving preserves ``working/``.
    Returns whether canonical state changed; callers own commit boundaries.
    """
    timestamp = now if isinstance(now, str) else _iso(now or _now())
    if archived:
        cursor = db.execute(
            "UPDATE threads SET lifecycle_state = 'archived', "
            "archived_at = CASE "
            "WHEN lifecycle_state = 'archived' AND archived_reason = 'dormancy' "
            "THEN COALESCE(archived_at, ?) ELSE ? END, "
            "archived_reason = 'user', updated_at = ? "
            "WHERE id = ? AND COALESCE(lifecycle_state, 'active') != 'purged' "
            "AND NOT (COALESCE(lifecycle_state, 'active') = 'archived' "
            "AND COALESCE(archived_reason, '') = 'user')",
            (timestamp, timestamp, timestamp, sid),
        )
    else:
        cursor = db.execute(
            "UPDATE threads SET lifecycle_state = 'active', dormant_at = NULL, "
            "archived_at = NULL, archived_reason = NULL, updated_at = ? "
            "WHERE id = ? AND lifecycle_state = 'archived' "
            "AND archived_reason = 'user'",
            (timestamp, sid),
        )
    if cursor.rowcount:
        if archived:
            _rehome_on_end(db, sid, "archived")
            _withdraw_owned_decisions(db, sid, reason="owner thread archived")
            invalidate_markers_for_archive(db, sid)
            from plans import public
            public.on_child_thread_archived(db, sid, archived_at=timestamp)
        else:
            _announce_lifecycle(db, sid)
        return True

    # Classify a no-op from current truth after the guarded write: a purge that
    # won the race must fail loudly, not be overwritten from a stale read.
    row = db.execute("SELECT lifecycle_state FROM threads WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise ThreadLifecycleError("thread does not exist")
    if (_row_get(row, "lifecycle_state") or "active") == "purged":
        raise ThreadLifecycleError("purged thread cannot be archived or unarchived")
    return False


SETTLED_WORKER_REASON = "assignment_settled"


def archive_settled_worker(db, sid: str, *, now: datetime | str | None = None) -> bool:
    """Archive a temporary worker whose assignment settled; the sweep owns cleanup.

    The third synchronous archive reason after the user's and the engine's:
    the plan's, for a spawned worker with nothing left to do for the step that
    spawned it. A root thread or a durable roster owner (``agent_enabled``) is
    never retired this way. Like a user archive this changes lifecycle state
    and withdraws the worker's open decisions; ``working/`` remains until
    permanent purge, so nothing the accepted result relies on is deleted here.
    Returns whether the state changed.
    """
    timestamp = now if isinstance(now, str) else _iso(now or _now())
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'archived', archived_at = ?, "
        "archived_reason = ?, updated_at = ? "
        "WHERE id = ? AND spawned_by IS NOT NULL AND COALESCE(agent_enabled, 0) = 0 "
        "AND COALESCE(lifecycle_state, 'active') IN ('active', 'dormant')",
        (timestamp, SETTLED_WORKER_REASON, timestamp, sid),
    )
    if not cursor.rowcount:
        return False
    _rehome_on_end(db, sid, "archived")
    _withdraw_owned_decisions(db, sid, reason="assignment settled")
    return True


def reactivate_settled_worker(db, sid: str, *, now: datetime | str | None = None) -> bool:
    """Bring a retired worker back for an explicit new assignment -- only that reason."""
    timestamp = now if isinstance(now, str) else _iso(now or _now())
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'active', dormant_at = NULL, "
        "archived_at = NULL, archived_reason = NULL, updated_at = ? "
        "WHERE id = ? AND lifecycle_state = 'archived' AND archived_reason = ?",
        (timestamp, sid, SETTLED_WORKER_REASON),
    )
    return bool(cursor.rowcount)


def _apply_archive(db, sid: str, now: datetime, reason: str) -> None:
    """Mark archived without deleting files or retiring their identities.

    ``reason`` is the archive provenance ('user' — the user set the archive flag;
    'dormancy' — the engine aged it in). It's what lets a later un-archive be
    recognized as a deliberate keep without also reactivating never-touched
    dormancy-archived threads (which would defeat dormancy-purge).

    User-initiated archive transitions are already recorded synchronously by
    ``set_user_archive_state``; this helper owns the retention transition and
    no physical cleanup.
    """
    cursor = db.execute(
        "UPDATE threads SET lifecycle_state = 'archived', archived_at = ?, "
        "archived_reason = ? WHERE id = ? "
        "AND COALESCE(lifecycle_state, 'active') != 'archived'",
        (_iso(now), reason, sid),
    )
    if cursor.rowcount:
        _rehome_on_end(db, sid, "archived")
        _withdraw_owned_decisions(db, sid, reason="owner thread archived")


def _apply_purge(db, sid: str, now: datetime) -> None:
    """Commit permanent unavailability and coupled SQL before physical cleanup."""
    from registry import models as registry_models

    _rehome_on_end(db, sid, "purged")
    registry_models.retire_owned_temporary(sid, db=db)
    _withdraw_owned_decisions(db, sid, reason="owner thread purged")
    _wake_parent_of_ended_child(db, sid, reason="purged")
    delete_thread_checkpoints(db, sid)
    delete_thread_continuity(db, sid)
    # One rule: held input dies exactly when the transcript dies, never
    # otherwise. Held input rides the reactor's tables, so its one teardown
    # door drops all of them; nothing survives a purge a reversible archive keeps.
    delete_thread_turns(db, sid)
    delete_thread_presence(db, sid)
    delete_thread_updates(db, sid)
    db.execute("DELETE FROM messages WHERE thread_id = ?", (sid,))
    bump_history_epoch(db, sid)
    db.execute(
        "UPDATE threads SET lifecycle_state = 'purged', purged_at = ? WHERE id = ?",
        (_iso(now), sid),
    )


def _evaluate_thread(
    db,
    row,
    now: datetime,
    cfg: LifecycleConfig,
    report: RetentionReport,
) -> str:
    sid = _row_get(row, "id")
    state = _row_get(row, "lifecycle_state") or "active"
    if state == "purged":
        return state  # terminal
    pinned = bool(_row_get(row, "pinned") or 0)
    archived_reason = _row_get(row, "archived_reason")
    dormant_at = _parse_ts(_row_get(row, "dormant_at"))
    archived_at = _parse_ts(_row_get(row, "archived_at"))

    last = _parse_ts(_row_get(row, "last_activity")) or _parse_ts(
        _row_get(row, "created_at")
    )
    idle_days = (now - last).days if last else 0

    # Retention transitions are not one-way: fresh activity reactivates a
    # dormant or dormancy-archived thread -- never a user archive (only the
    # user's unarchive does) nor a settled worker's (only a new assignment does).
    resumed = (
        state in ("dormant", "archived")
        and archived_reason not in ("user", SETTLED_WORKER_REASON)
        and idle_days < cfg.dormant_after_days
    )
    if resumed:
        db.execute(
            "UPDATE threads SET lifecycle_state = 'active', dormant_at = NULL, "
            "archived_at = NULL, archived_reason = NULL WHERE id = ?",
            (sid,),
        )
        state = "active"
        dormant_at = None
        archived_at = None
        archived_reason = None
        report.reactivated.append(sid)

    # active -> dormant (idle >= threshold). State only; nothing physical.
    if state == "active" and idle_days >= cfg.dormant_after_days:
        _set_dormant(db, sid, now)
        dormant_at = now
        state = "dormant"
        report.dormant.append(sid)

    # dormant -> archived after the retention threshold. User archives already
    # enter this state synchronously through ``set_user_archive_state``.
    if state == "dormant":
        by_dormancy = (
            dormant_at is not None
            and (now - dormant_at).days >= cfg.archive_after_days
        )
        if by_dormancy:
            _apply_archive(db, sid, now, "dormancy")
            archived_at = now
            state = "archived"
            report.archived.append(sid)

    # -> permanently purged: 180d archived, unless pinned. Files are removed after commit.
    if state == "archived" and not pinned:
        if archived_at is not None and (now - archived_at).days >= cfg.purge_after_days:
            _apply_purge(db, sid, now)
            state = "purged"
            report.purged.append(sid)

    return state


def _evaluate_candidate(
    db,
    sid: str,
    now: datetime,
    cfg: LifecycleConfig,
    workspace: Path,
    report: RetentionReport,
) -> None:
    """Revalidate under the writer; only committed purges may remove files."""
    with write_transaction(db, scope="thread_lifecycle_candidate"):
        row = db.execute(
            "SELECT s.id, s.project_id, s.lifecycle_state, s.dormant_at, "
            "s.archived_at, s.archived_reason, s.pinned, s.agent_enabled, "
            "s.created_at, "
            "(SELECT MAX(m.created_at) FROM messages m "
            " WHERE m.thread_id = s.id) AS last_activity "
            "FROM threads s WHERE s.id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            return

        if bool(_row_get(row, "agent_enabled") or 0):
            state = _row_get(row, "lifecycle_state") or "active"
        else:
            state = _evaluate_thread(db, row, now, cfg, report)

    if state == "purged":
        from registry import service as registry_service

        thread_root = _thread_dir(db, sid, _row_get(row, "project_id"), workspace)
        if thread_root is None:
            return
        try:
            _remove_tree(thread_root)
        except OSError as exc:
            # The purge is committed and a later sweep retries its retained
            # bytes; one stuck tree must not stop the other candidates or trims.
            report.purge_cleanup_errors += 1
            logger.warning("retention kept purged thread {} files: {}", sid, exc)
            return
        registry_service.retire_missing_temporary_under(
            thread_root, parent_thread_id=sid, db=db, include_archived=True,
        )


def run_retention_sweep(
    db,
    *,
    now: datetime | None = None,
    config: LifecycleConfig | None = None,
    workspace: Path | None = None,
) -> RetentionReport:
    """Apply all lifecycle transitions + retention trims for one scope's DB.

    Guarded per-row by state/timestamp; a re-run finishes retained purge bytes
    without repeating lifecycle transitions. Excludes agent-roster threads from the state
    machine; never deletes summaries.
    """
    if db.in_transaction:
        raise TransactionOwnershipError("retention sweep requires a clean connection before filesystem work")
    if now is None:
        now = _now()
    if config is None:
        config = LifecycleConfig()
    if workspace is None:
        from infra.config import WORKSPACE

        workspace = WORKSPACE

    report = RetentionReport()

    # Purged rows also let this existing owner finish interrupted physical cleanup.
    candidates = db.execute("SELECT id FROM threads").fetchall()
    for candidate in candidates:
        _evaluate_candidate(
            db,
            _row_get(candidate, "id"),
            now,
            config,
            workspace,
            report,
        )

    from core.thread_lifecycle_trims import _trim_tool_payloads, _trim_working_tmp

    _trim_tool_payloads(db, now, config, report)
    _trim_working_tmp(db, now, config, workspace, report)

    return report


def run_lifecycle_retention() -> dict:
    """Entry point for the scheduler: open the app DB, run one sweep, log."""
    from infra.database import get_db

    db = get_db()
    try:
        report = run_retention_sweep(db)
    finally:
        db.close()
    counts = report.as_dict()
    logger.info("lifecycle_retention: {}", " ".join(
        f"{k}={len(v) if isinstance(v, list) else v}" for k, v in counts.items()))
    return counts
