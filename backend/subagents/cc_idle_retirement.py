"""Working-process accounting and silent idle retirement for Claude Code.

A held Claude Code handle is *working* while it runs a Turn (in memory or as
the durable ``active_turns`` row), owns admitted tool tasks or open tool
steps, holds an unsettled process exit, or is inside a transition such as
spawn or resume (its transition lock is held). The delegation cap counts only
working handles (issue #2064).

This module is the one owner that retires idle handles: a single periodic
pass closes a handle through the existing ``close_and_finalize`` under the
thread's transition lock, without a closed notice, so the thread is left
exactly as after a backend restart and its next input cold-resumes it. The
pass never retires a handle whose thread still has a pending idle-checkpoint
marker or an active checkpoint, so the warm idle checkpoint always runs first.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infra.database import get_db
from subagents.cc_runtime_state import _LIVE, _Live

log = logging.getLogger("subagents.cc_runtime")

#: Seconds between retirement passes. Scheduling cadence only; the retirement
#: policy numbers live in ``infra/subagent_caps.json``.
PASS_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class IdleCandidate:
    """One idle handle whose every non-time retirement condition holds."""

    thread_id: str
    live: _Live
    idle_since: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _busy_in_memory(live: _Live) -> bool:
    return bool(
        live.turn_active
        or live.admitted_tool_tasks
        or live.tool_step_ids
        or live.pending_process_exit is not None
    )


def _transition_held(thread_id: str) -> bool:
    lock = _LIVE.existing_lock(thread_id)
    return lock is not None and lock.locked()


def _is_working(
    thread_id: str, live: _Live | None, running: set[str], *, lock_held: bool = False,
) -> bool:
    """Return whether a live handle is ineligible for idle retirement now."""
    return (
        live is None
        or _LIVE.get(thread_id) is not live
        or thread_id in running
        or _busy_in_memory(live)
        or (not lock_held and _transition_held(thread_id))
    )


def working_thread_ids(*, db=None) -> tuple[str, ...]:
    """Return the ids of held Claude Code handles that are working now.

    Reads borrow the caller's pooled connection and never close it: closing
    rolls back whatever write the calling frame still has pending.
    """
    from core.turn_core import active_turn_thread_ids

    running = set(active_turn_thread_ids(db=db if db is not None else get_db()))
    working = []
    for thread_id in _LIVE.ids():
        live = _LIVE.get(thread_id)
        if _is_working(thread_id, live, running):
            working.append(thread_id)
    return tuple(working)


def thread_titles(thread_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Pair each thread id with its title for an inspectable refusal."""
    if not thread_ids:
        return ()
    marks = ",".join("?" for _ in thread_ids)
    rows = get_db().execute(
        f"SELECT id,title FROM threads WHERE id IN ({marks})", thread_ids,
    ).fetchall()
    titles = {str(row["id"]): row["title"] for row in rows}
    return tuple(
        (thread_id, str(titles.get(thread_id) or "Untitled thread"))
        for thread_id in thread_ids
    )


def _durable_idle_since(connection, thread_id: str) -> datetime | None:
    """When the thread's last Turn ended, or None while anything durable is open.

    Open means a running Turn, a pending idle-checkpoint marker, an active
    checkpoint, or no ended Turn to measure idleness from.
    """
    from infra.checkpoint_queries import active_for_thread

    row = connection.execute(
        "SELECT "
        "(SELECT ended_at FROM active_turns WHERE thread_id=:t "
        " ORDER BY turn_order DESC LIMIT 1) AS last_ended, "
        "EXISTS(SELECT 1 FROM active_turns WHERE thread_id=:t "
        " AND status IN ('running','waiting_retry')) AS running, "
        "EXISTS(SELECT 1 FROM active_turns WHERE thread_id=:t "
        " AND idle_checkpoint_pending=1) AS marker",
        {"t": thread_id},
    ).fetchone()
    open_durable_work = any((row["running"], row["marker"]))
    if open_durable_work or row["last_ended"] is None:
        return None
    if active_for_thread(thread_id, db=connection) is not None:
        return None
    return _naive_utc(str(row["last_ended"]))



def _read_retirement_snapshot(thread_ids: tuple[str, ...]):
    """Read the durable retirement policy and state in a DB worker."""
    from infra.subagent_caps import get_idle_retire_after_seconds, get_max_idle_live

    connection = get_db()
    working = set(working_thread_ids(db=connection))
    durable_idle = {
        thread_id: _durable_idle_since(connection, thread_id)
        for thread_id in thread_ids if thread_id not in working
    }
    return get_idle_retire_after_seconds(), get_max_idle_live(), working, durable_idle


async def _survey_for_retirement() -> tuple[int | None, int | None, int, list[IdleCandidate]]:
    """Survey live handles without doing SQLite work on the event loop."""
    from infra.db_async import run_in_db

    snapshot = tuple((thread_id, _LIVE.get(thread_id)) for thread_id in _LIVE.ids())
    thread_ids = tuple(thread_id for thread_id, live in snapshot if live is not None)
    idle_after_s, max_idle, working, durable_idle = await run_in_db(
        _read_retirement_snapshot, thread_ids,
    )
    idle: list[tuple[str, _Live]] = []
    candidates = []
    for thread_id, live in snapshot:
        if _is_working(thread_id, live, working):
            continue
        idle.append((thread_id, live))
        if (since := durable_idle.get(thread_id)) is not None:
            candidates.append(IdleCandidate(thread_id, live, since))
    candidates.sort(key=lambda candidate: candidate.idle_since)
    return idle_after_s, max_idle, len(idle), candidates


def _current_durable_idle_since(thread_id: str) -> datetime | None:
    """Read one candidate's durable eligibility from a DB worker."""
    return _durable_idle_since(get_db(), thread_id)


def select_for_retirement(
    idle_count: int,
    candidates: list[IdleCandidate],
    *,
    now: datetime,
    idle_after_s: int,
    max_idle: int,
) -> list[IdleCandidate]:
    """Choose idle-expired handles, then the longest-idle beyond the bound."""
    cutoff = now - timedelta(seconds=idle_after_s)
    expired = [c for c in candidates if c.idle_since <= cutoff]
    recent = [c for c in candidates if c.idle_since > cutoff]
    excess = max(0, idle_count - len(expired) - max_idle)
    return expired + recent[:excess]


async def retire_candidate(candidate: IdleCandidate) -> bool:
    """Silently close one handle if it is still exactly the idle candidate."""
    from subagents.cc_handle_close import close_and_finalize
    from subagents.cc_runtime_helpers import _default_broadcast
    from subagents.cc_runtime_state import purge_tool_token_grace
    from subagents.live_registry import StaleTransition

    thread_id, live = candidate.thread_id, candidate.live
    lock = _LIVE.lock(thread_id)
    if lock.locked():
        return False
    try:
        async with lock:
            if _is_working(thread_id, live, set(), lock_held=True):
                return False
            from infra.db_async import run_in_db

            since = await run_in_db(_current_durable_idle_since, thread_id)
            if since != candidate.idle_since:
                return False
            closed = await close_and_finalize(
                thread_id, live, _default_broadcast, emit_closed=False,
            )
    except StaleTransition:
        return False
    if closed:
        purge_tool_token_grace(thread_id)
        log.info("cc_runtime: retired idle process for %s", thread_id)
    return closed


async def retire_idle_once(*, now: datetime | None = None) -> tuple[str, ...]:
    """Run one retirement pass and return the retired thread ids."""
    idle_after_s, max_idle, idle_count, candidates = await _survey_for_retirement()
    if idle_after_s is None or max_idle is None:
        log.warning("cc_runtime: idle retirement unconfigured; retaining idle processes")
        return ()
    selected = select_for_retirement(
        idle_count, candidates,
        now=now or _utc_now(), idle_after_s=idle_after_s, max_idle=max_idle,
    )
    retired = []
    for candidate in selected:
        try:
            if await retire_candidate(candidate):
                retired.append(candidate.thread_id)
        except Exception:
            log.exception("cc_runtime: idle retirement failed for %s", candidate.thread_id)
    return tuple(retired)


async def idle_retirement_loop() -> None:
    """Run the retirement pass forever; the app lifespan cancels it at shutdown."""
    while True:
        await asyncio.sleep(PASS_INTERVAL_S)
        try:
            await retire_idle_once()
        except Exception:
            log.exception("cc_runtime: idle retirement pass failed")
