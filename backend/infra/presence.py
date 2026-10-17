"""User presence: when a conversation is actually in front of the user.

The single owner of presence state (issue #2004; design
``shared/substrate/presence-design-2026-09-18.md``):

- **Claims**, in process memory: a session (one WebSocket) asserting one
  surface is in front of the user. The client decides; the server stamps it.
- **The visit record**, ``thread_presence``: one row per (user, thread). A
  visit runs from its first claim to final release; a re-claim inside the 2 min
  merge window stays in the visit.
- **Visit history**, ``presence_visits``: one row per finalized visit.

Nothing here admits input or starts a Turn. Every entry point is best-effort
for its caller: a failed presence write is logged by the caller and dropped,
and the next transition or sweep converges the row.

Exit stamps are back-dated to when the user stopped being here (the hidden
time, the last input, the last confirmation) and stored as
``exit_pending_at``. The merge window runs from ``exit_pending_since``, the
server time the exit was detected, so a back-dated stamp never shortens the
window a reconnecting client has to re-claim.

Claims live in one process, the same assumption the WebSocket subscriptions
make; more than one backend process would need claims in the database.

Record writers return the (user, thread) pairs whose row committed, for an
after-commit dot nudge; a newly opened visit clears ``mark_seen``'s marks.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import infra.measurement_focus as measurement_focus
import infra.presence_records as presence_records
from infra.database import write_transaction
from infra.presence_time import iso, parse, seen_boundary
from infra.visit_divider import divider_from_row

CHAT_SURFACE = "chat"
ACCEPTED_SURFACES = frozenset({CHAT_SURFACE})
HIDDEN_GRACE = timedelta(seconds=30)
MERGE_WINDOW = timedelta(minutes=2)
KEEPALIVE_INTERVAL = timedelta(minutes=5)
EXIT_REASONS = frozenset({"navigated", "hidden", "idle", "disconnected", "restart"})

_Key = tuple[str, str, str]  # (user_id, surface, thread_id)
Changed = list[tuple[str, str]]  # (user_id, thread_id) whose record committed


@dataclass(frozen=True)
class DocumentOpenSnapshot:
    """One session-local capability snapshot for an agent document request."""

    verdict: str
    eligible_session_ids: frozenset[str]
    already_open: bool


@dataclass(frozen=True)
class _DocumentOpenCapability:
    can_show: bool
    visible_artifact_id: str | None


@dataclass
class _Visit:
    """In-memory half of one visit: who claims it now and when they left."""

    claims: dict[str, datetime] = field(default_factory=dict)
    hidden: dict[str, datetime] = field(default_factory=dict)
    document_open: dict[str, _DocumentOpenCapability] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)
    left_at: datetime | None = None
    left_reason: str | None = None
    exit_written: bool = False
    divider_frozen: bool = False
    divider: dict | None = None

_LOCK = threading.RLock()
_visits: dict[_Key, _Visit] = {}
_session_keys: dict[tuple[str, str], _Key] = {}


def frozen_visit_divider(user_id: str, thread_id: str) -> tuple[bool, dict | None]:
    """Return an open chat visit's pre-entry divider, including a frozen None."""
    with _LOCK:
        visit = _visits.get((user_id, CHAT_SURFACE, thread_id))
        return (True, visit.divider) if visit and visit.claims and visit.divider_frozen else (False, None)


def _require_surface(surface: str) -> None:
    if surface not in ACCEPTED_SURFACES:
        raise ValueError(f"unsupported presence surface: {surface!r}")


def claim(
    db: sqlite3.Connection, *, user_id: str, surface: str, thread_id: str,
    session: str, now: datetime, focused: bool | None = None,
) -> Changed:
    """Record that ``session`` now shows ``thread_id`` on ``surface``.

    A session shows one object per surface, so a claim for a different thread
    first releases the old one as a navigation. The first live claim of a
    visit opens it, or continues it when the row is still inside its merge
    window; further sessions are memory only.
    """
    _require_surface(surface)
    key = (user_id, surface, thread_id)
    changed: Changed = []
    with _LOCK:
        previous = _session_keys.get((session, surface))
        if previous is not None and previous != key:
            _release(db, session=session, surface=surface,
                     reason="navigated", stamp=now, now=now, changed=changed)
        visit = _visits.setdefault(key, _Visit())
        needs_divider = not visit.claims and not visit.divider_frozen
    divider = divider_from_row(db, presence_records.row(db, user_id, thread_id), thread_id) if needs_divider else None
    with _LOCK:
        visit = _visits.setdefault(key, _Visit())
        opening = not visit.claims
        if opening and not visit.divider_frozen:
            visit.divider = divider if needs_divider else divider_from_row(db, presence_records.row(db, user_id, thread_id), thread_id)
            visit.divider_frozen = True
        visit.claims.setdefault(session, now)
        visit.hidden.pop(session, None)
        visit.sessions.add(session)
        visit.left_at = visit.left_reason = None
        visit.exit_written = False
        _session_keys[(session, surface)] = key
        if opening:
            presence_records.enter(db, user_id, thread_id, now)
            changed.append((user_id, thread_id))
        if focused is not None:
            measurement_focus.set_focused(
                db, user_id=user_id, surface=surface, thread_id=thread_id,
                session=session, focused=focused, now=now,
            )
    return changed


def release(
    db: sqlite3.Connection, *, session: str, surface: str, reason: str,
    now: datetime, idle_for: timedelta | None = None,
) -> Changed:
    """Release ``session``'s claim on ``surface``, stamped when the user left.

    ``reason`` is ``navigated``, ``hidden`` or ``idle``. A hidden page leaves at
    once for the agent's view but waits out the 30 s grace before the record
    starts its exit. An idle release is stamped ``now - idle_for``, never
    before that session's claim.
    """
    _require_surface(surface)
    changed: Changed = []
    with _LOCK:
        stamp = now
        key = _session_keys.get((session, surface))
        if key is not None and idle_for is not None:
            claimed = _visits[key].claims.get(session, now)
            stamp = max(now - idle_for, claimed)
        _release(db, session=session, surface=surface,
                 reason=reason, stamp=stamp, now=now, changed=changed)
    return changed


def update_document_open_capability(
    *, user_id: str, thread_id: str, session: str, can_show: bool,
    visible_artifact_id: str | None,
) -> bool:
    """Remember one claimed session's delivery capability without touching its visit."""
    key = (user_id, CHAT_SURFACE, thread_id)
    capability = _DocumentOpenCapability(can_show, visible_artifact_id)
    with _LOCK:
        if _session_keys.get((session, CHAT_SURFACE)) != key:
            return False
        visit = _visits.get(key)
        if visit is None or session not in visit.claims or visit.document_open.get(session) == capability:
            return False
        visit.document_open[session] = capability
        return True


def document_open_snapshot(user_id: str, thread_id: str, artifact_id: str) -> DocumentOpenSnapshot:
    """Return current eligible deliveries and whether a live session already shows it."""
    with _LOCK:
        verdict = _live_view_locked(user_id, thread_id)
        if verdict is None:
            return DocumentOpenSnapshot("unknown", frozenset(), False)
        if verdict[0] != "here":
            return DocumentOpenSnapshot("away", frozenset(), False)
        visit = _visits[(user_id, CHAT_SURFACE, thread_id)]
        capabilities = {
            session: visit.document_open.get(session)
            for session in visit.claims
        }
        return DocumentOpenSnapshot(
            "here",
            frozenset(session for session, capability in capabilities.items() if capability and capability.can_show),
            any(capability and capability.visible_artifact_id == artifact_id for capability in capabilities.values()),
        )


def release_session(db: sqlite3.Connection, *, session: str, now: datetime) -> Changed:
    """Release every claim of a closed socket, stamped at the close."""
    changed: Changed = []
    with _LOCK:
        for surface in [s for (sid, s) in _session_keys if sid == session]:
            _release(db, session=session, surface=surface,
                     reason="disconnected", stamp=now, now=now, changed=changed)
    return changed


def _release(
    db: sqlite3.Connection, *, session: str, surface: str, reason: str,
    stamp: datetime, now: datetime, changed: Changed,
) -> None:
    if reason not in EXIT_REASONS:
        raise ValueError(f"unknown presence exit reason: {reason!r}")
    key = _session_keys.pop((session, surface), None)
    if key is None:
        return
    visit = _visits[key]
    measurement_focus.end_session(db, session=session, surface=surface, now=stamp)
    visit.claims.pop(session, None)
    visit.document_open.pop(session, None)
    if reason == "hidden":
        visit.hidden[session] = stamp
    else:
        _note_left(visit, stamp, reason)
    _settle(db, key, visit, now, changed)


def _note_left(visit: _Visit, stamp: datetime, reason: str) -> None:
    if visit.left_at is None or stamp >= visit.left_at:
        visit.left_at, visit.left_reason = stamp, reason


def _settle(db: sqlite3.Connection, key: _Key, visit: _Visit, now: datetime,
            changed: Changed) -> None:
    """Start the recorded exit once no claim is live and every grace has ended."""
    if visit.claims:
        return
    for session, hidden_at in list(visit.hidden.items()):
        if now - hidden_at >= HIDDEN_GRACE:
            del visit.hidden[session]
            _note_left(visit, hidden_at, "hidden")
    if visit.hidden or visit.left_at is None or visit.exit_written:
        return
    user_id, _surface, thread_id = key
    if presence_records.mark_exit_pending(db, user_id, thread_id, visit.left_at,
                                          visit.left_reason or "navigated", now):
        changed.append((user_id, thread_id))
    visit.exit_written = True


def _live_view_locked(user_id: str, thread_id: str) -> tuple[str, datetime | None] | None:
    """Read one process-local visit while its lock is already held."""
    visit = _visits.get((user_id, CHAT_SURFACE, thread_id))
    if visit is None:
        return None
    if visit.claims:
        return "here", None
    stamps = [*visit.hidden.values(), visit.left_at]
    stamp = max((value for value in stamps if value is not None), default=None)
    return ("away", stamp) if stamp is not None else None


def live_view(user_id: str, thread_id: str) -> tuple[str, datetime | None] | None:
    """Return the canonical process-local visit verdict without a read-model cycle."""
    with _LOCK:
        return _live_view_locked(user_id, thread_id)



def sweep(db: sqlite3.Connection, *, now: datetime) -> Changed:
    """Advance every visit whose timer ran out; safe to run at any cadence.

    Ends hidden graces, finalizes exits whose merge window has passed since
    detection, refreshes ``last_confirmed_at`` for live visits at most every
    5 min, and forgets memory for visits the record no longer holds open.
    """
    changed: Changed = []
    with _LOCK:
        for key, visit in list(_visits.items()):
            _settle(db, key, visit, now, changed)
        cutoff = iso(now - MERGE_WINDOW)
        for row in db.execute(
            "SELECT * FROM thread_presence WHERE present=1 "
            "AND exit_pending_since IS NOT NULL AND exit_pending_since <= ?",
            (cutoff,),
        ).fetchall():
            key = (row["user_id"], CHAT_SURFACE, row["thread_id"])
            pending = _visits.get(key)
            if pending is not None and (pending.claims or pending.hidden):
                continue
            presence_records.finalize(db, row, len(pending.sessions) if pending else 1, now, CHAT_SURFACE)
            changed.append((row["user_id"], row["thread_id"]))
            _visits.pop(key, None)
        _keep_alive(db, now)
        _forget_closed(db)
    return changed


def _keep_alive(db: sqlite3.Connection, now: datetime) -> None:
    stale = iso(now - KEEPALIVE_INTERVAL)
    live = [key for key, visit in _visits.items() if visit.claims]
    if not live:
        return
    with write_transaction(db, scope="presence_keep_alive"):
        for user_id, _surface, thread_id in live:
            db.execute(
                "UPDATE thread_presence SET last_confirmed_at=?, updated_at=? "
                "WHERE user_id=? AND thread_id=? AND present=1 "
                "AND last_confirmed_at <= ?",
                (iso(now), iso(now), user_id, thread_id, stale),
            )


def _forget_closed(db: sqlite3.Connection) -> None:
    """Drop released visits whose row is gone or already closed (thread deleted)."""
    for key, visit in list(_visits.items()):
        if visit.claims or visit.hidden:
            continue
        row = presence_records.row(db, key[0], key[2])
        if row is None or not row["present"]:
            del _visits[key]


def reconcile_startup(db: sqlite3.Connection, *, now: datetime) -> None:
    """After a restart no claim survives: every open visit becomes exit-pending.

    The exit is stamped at the last confirmation (or the exit already
    pending), and the merge window restarts now, so a browser that reconnects
    within 2 min continues the same visit.
    """
    with _LOCK:
        _visits.clear()
        _session_keys.clear()
        measurement_focus.reconcile_startup(db, now=now)
        with write_transaction(db, scope="presence_reconcile"):
            db.execute(
                "UPDATE thread_presence SET "
                "exit_pending_at=COALESCE(exit_pending_at, last_confirmed_at), "
                "exit_pending_since=?, "
                "exit_pending_reason=COALESCE(exit_pending_reason, 'restart'), "
                "updated_at=? WHERE present=1",
                (iso(now), iso(now)),
            )


def mark_seen(
    db: sqlite3.Connection, *, user_id: str, thread_id: str, unread: bool, now: datetime,
) -> bool:
    """Mark read through ``now`` or unread; False with no record, never a new visit."""
    stamp = iso(now)
    with write_transaction(db, scope="presence_mark_seen"):
        return db.execute(
            "UPDATE thread_presence SET marked_read_at=CASE WHEN ? THEN marked_read_at ELSE ? END, "
            "marked_unread_at=CASE WHEN ? THEN ? END, updated_at=? WHERE user_id=? AND thread_id=?",
            (unread, stamp, unread, stamp, stamp, user_id, thread_id),
        ).rowcount == 1


def delete_thread_presence(db: sqlite3.Connection, thread_id: str) -> None:
    """Delete a thread's presence record and visit history (thread delete, purge).

    Runs inside the caller's transaction and never takes the claims lock, so
    it cannot invert lock order with a presence write waiting on that
    transaction; the next sweep forgets any memory left for the thread.
    """
    db.execute("DELETE FROM thread_presence WHERE thread_id=?", (thread_id,))
    db.execute("DELETE FROM presence_visits WHERE thread_id=?", (thread_id,))
    measurement_focus.delete_thread(db, thread_id)
