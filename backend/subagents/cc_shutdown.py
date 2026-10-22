"""Explicit retirement and backend shutdown for Claude Code handles."""

from __future__ import annotations

import asyncio
import logging
import uuid

from infra.db_async import run_in_db
from infra.turn_errors import insert_turn_error
from infra.queries import insert_message
from llm.error_cause import cause_fields
from subagents.cc_handle_close import (
    close_handle,
    finalize,
    settle_and_finalize_retired_live_for_identity,
)
from subagents.cc_runtime_helpers import _default_broadcast, _proc_is_alive
from subagents.cc_runtime_quiescence import join_admitted_tool_tasks, prepare_live_close
from subagents.cc_runtime_state import _CLOSE_LIVE_TIMEOUT_S, _LIVE, _Live

log = logging.getLogger("subagents.cc_runtime")


def retire_provider_session(child_thread_id: str, *, db=None) -> bool:
    """Retire the durable Claude transcript after a context-truth change."""
    from infra.database import write_transaction
    from registry import models

    with write_transaction(db, scope="retire_cc_provider_session") as connection:
        row = models.find_for_thread(
            child_thread_id, "claude-code", "session", db=connection,
        )
        if row is None:
            return False
        models.retire(row["id"], db=connection)
        return True


async def retire_live(child_thread_id: str, broadcast=None) -> bool:
    """Unconditionally retire a handle for delete/model-boundary teardown."""
    live = _LIVE.get(child_thread_id)
    if live is None:
        return False
    return await close_handle(
        child_thread_id, live, broadcast, expected_identity=live.corsia_turn
    )


async def retire(child_thread_id: str, *, broadcast=None) -> bool:
    """Serialize an explicit unconditional destructive retirement."""
    async with _LIVE.lock(child_thread_id):
        had_live = _LIVE.get(child_thread_id) is not None
        retired_live = await retire_live(child_thread_id, broadcast)
        if had_live and not retired_live:
            return False
        retired_provider = await run_in_db(retire_provider_session, child_thread_id)
        return retired_provider or retired_live


def is_live(child_thread_id: str) -> bool:
    """Return whether a Claude Code thread has a running provider process."""
    live = _LIVE.get(child_thread_id)
    return live is not None and _proc_is_alive(live.session)


def has_pending_or_live(thread_id: str) -> bool:
    """Return whether a Claude Code thread is pending spawn or live."""
    lock = _LIVE.existing_lock(thread_id)
    return thread_id in _LIVE or (lock is not None and lock.locked())


async def _terminate_for_shutdown(
    child_id: str, live: _Live, deadline: float
) -> tuple[bool, asyncio.CancelledError | None]:
    if live.session is None:
        return True, None
    terminate = getattr(live.session, "terminate", None)
    if terminate is None:
        log.error("cc_runtime: shutdown retained %s; process death unproven", child_id)
        return False, None
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        await asyncio.wait_for(terminate(), timeout=remaining)
        return True, None
    except asyncio.TimeoutError:
        log.error("cc_runtime: shutdown retained %s; terminate timed out", child_id)
    except asyncio.CancelledError as exc:
        log.error("cc_runtime: shutdown retained %s; terminate cancelled", child_id)
        return False, exc
    except Exception:
        log.exception("cc_runtime: shutdown retained %s; terminate failed", child_id)
    return False, None


def _record_shutdown_marker(child_id: str, turn_id: str | None) -> None:
    try:
        insert_turn_error(
            insert_message, child_id, "Terminated by backend shutdown.",
            {
                "cc": True, "type": "cc_error",
                "event_type": "harness.error", "harness": "claude-code",
                **cause_fields("claude-code", "backend_shutdown"), "shutdown": True,
            },
            turn_id=turn_id, msg_id=str(uuid.uuid4()),
        )
    except Exception as exc:
        log.warning("shutdown marker failed for %s: %s", child_id, exc)


async def shutdown_one_live(child_id: str, live: _Live) -> None:
    """Quiesce and finalize one exact Claude Code live or retain its guard."""
    identity = live.corsia_turn
    prepared = await prepare_live_close(
        child_id, live, identity, timeout_s=_CLOSE_LIVE_TIMEOUT_S
    )
    if prepared is None:
        return
    deadline, admitted_tool_tasks = prepared
    terminated, cancellation = await _terminate_for_shutdown(
        child_id, live, deadline
    )
    quiesced = await join_admitted_tool_tasks(
        live, admitted_tool_tasks, deadline=deadline
    )
    if cancellation is not None:
        raise cancellation
    if not terminated or not quiesced:
        log.error("cc_runtime: shutdown retained %s without quiescence proof", child_id)
        return
    was_active = live.turn_active
    replaying_natural_exit = live.pending_process_exit is not None
    finalized = await settle_and_finalize_retired_live_for_identity(
        child_id, live, _default_broadcast, identity, None,
        emit_closed=False, emit_terminal=False, finalize_callback=finalize,
    )
    if (
        finalized and was_active and not replaying_natural_exit
        and not live.turn_active and live.parent_thread_id
    ):
        await run_in_db(
            _record_shutdown_marker, child_id, getattr(identity, "turn_id", None),
        )


async def shutdown_all() -> None:
    """Stop every Claude Code runtime without one failure skipping the rest."""
    cancellation: asyncio.CancelledError | None = None
    for child_id in _LIVE.ids():
        live = _LIVE.get(child_id)
        if live is None:
            continue
        try:
            await shutdown_one_live(child_id, live)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            log.error("cc_runtime: shutdown cancellation retained %s", child_id)
        except Exception:
            log.exception("cc_runtime: shutdown retained failed live %s", child_id)
    if cancellation is not None:
        raise cancellation
    remaining = _LIVE.ids()
    if remaining:
        raise RuntimeError(
            "shutdown retained Claude Code runtimes: " + ", ".join(remaining)
        )
