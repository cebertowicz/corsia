"""Claude Code handle transitions performed before one input is accepted."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from infra.database import get_db
from infra.db_async import run_in_db
from llm.resolution import ExecutionTarget
from subagents.cc_destination import prepared_destination_from_context
from subagents.cc_replay_authority import require_cc_provider_fit
from subagents.cc_resume_transition import (
    replace_same_turn_after_checkpoint_locked,
    resume_child,
    retire_rejected_provider_session,
)
from subagents.cc_runtime_errors import (
    CCLiveTurnBusy,
    CCPreparedDestinationStale,
)
from subagents.cc_runtime_helpers import (
    CCWorkingDirMissing,
    _main_working_dir,
    _proc_is_alive,
    native_stale_working_dir,
)
from subagents.cc_runtime_lifecycle import close_and_finalize, finalize
from subagents.cc_runtime_state import Broadcast, _Live
from subagents.cc_preempt import replay_pending_exit_before_preempt

if TYPE_CHECKING:
    from core.context_payload import PreparedContextPayload

log = logging.getLogger("subagents.cc_runtime")


@dataclass
class CCFeedTransition:
    """Own the prepared context and handle replacement for one input."""

    thread_id: str
    text: str
    target: ExecutionTarget
    effort: str | None
    claude_dir: str | None
    binary: str | None
    broadcast: Broadcast | None
    is_subagent: bool | None
    bcast: Broadcast
    db: Any
    prepared_context: PreparedContextPayload | None
    refresh_callback: Callable[[], Awaitable[PreparedContextPayload]] | None
    # The transition lock generation this input holds; only it may publish.
    held_lock: asyncio.Lock
    refreshed: bool = False

    async def refresh_once(self) -> PreparedContextPayload | None:
        """Refresh once at a safe boundary before binding the exact Turn."""
        if self.refresh_callback is not None and not self.refreshed:
            await self.refresh_current_context()
        return self.prepared_context

    async def refresh_current_context(self, selection=None) -> PreparedContextPayload:
        """Re-render the same FIFO input after its continuation changes."""
        if self.refresh_callback is None:
            raise RuntimeError("Claude Code continuation change cannot refresh input")
        refreshed = await (
            self.refresh_callback() if selection is None
            else self.refresh_callback(selection)
        )
        if refreshed is None:
            raise RuntimeError("Claude Code safe-boundary preflight returned no payload")
        self.prepared_context = refreshed
        self.refreshed = True
        return refreshed

    async def resume(self) -> _Live | None:
        """Start from the exact prepared context at the current boundary."""
        prepared_context = await self.refresh_once()
        return await resume_child(
            self.thread_id,
            target=self.target,
            effort=self.effort,
            claude_dir=self.claude_dir,
            binary=self.binary,
            broadcast=self.broadcast,
            is_subagent=self.is_subagent,
            current_user_text=self.text,
            prepared_context=prepared_context,
            db=self.db,
            held_lock=self.held_lock,
        )

    async def require_idle(self, live: _Live) -> _Live | None:
        """Reject input while the exact prior turn or exit still owns the handle."""
        if live.pending_process_exit is not None:
            await replay_pending_exit_before_preempt(
                self.thread_id, live, self.bcast, self.db
            )
            return None
        if live.turn_active:
            raise CCLiveTurnBusy(
                "Claude Code still owns the previous turn; defer this input "
                "through the thread inbox"
            )
        return live

    def require_direct_fit(self) -> None:
        """Refuse a direct caller's unproved context before acceptance."""
        if self.refresh_callback is None:
            require_cc_provider_fit(self.prepared_context, self.target)

    async def rebuild_rejected_resume(self, live: _Live):
        """Rebuild the bound Turn's handle from CORSIA history, same Turn.

        Claude refused the session unread, so its missing transcript is
        retired and this input is re-rendered as exact replay.
        """
        log.warning("cc_runtime: %s rejected resume; rebuilding", self.thread_id)
        await self.retire_rejected_session(live)
        if self.refresh_callback is None:
            raise RuntimeError(
                "Claude Code missing provider session requires exact re-preflight"
            )
        prepared_context = await self.refresh_current_context()
        require_cc_provider_fit(prepared_context, self.target)
        return await replace_same_turn_after_checkpoint_locked(
            self.thread_id, live, prepared_context, self.text,
            is_subagent=self.is_subagent, held_lock=self.held_lock,
            broadcast=self.broadcast,
        )

    async def retire_rejected_session(self, live: _Live) -> None:
        """Retire the provider session Claude reported missing, off the loop."""
        await run_in_db(
            retire_rejected_provider_session,
            self.thread_id, getattr(live.session, "resume_session_id", None), None,
        )

    async def replace_quarantined(self, live: _Live) -> _Live | None:
        """Replace a handle whose admitted tools make its context unusable."""
        if not (live.provider_context_untrusted and live.admitted_tool_tasks):
            return live
        if not await close_and_finalize(
            self.thread_id, live, self.bcast, self.db
        ):
            log.error(
                "CC %s refused input while its quarantined guard remains",
                self.thread_id,
            )
            return None
        return await self.resume()

    async def consume_cold_transition(self, live: _Live) -> _Live | None:
        """Replace an older live with this exact admitted cold preparation."""
        if getattr(self.prepared_context, "mode", None) not in {
            "exact_replay",
            "checkpoint_tail",
        }:
            return live
        destination = prepared_destination_from_context(
            self.thread_id,
            self.text,
            target=self.target,
            prepared_context=self.prepared_context,
        )
        if live.prepared_destination_identity is destination.launch_identity:
            return live
        if not await close_and_finalize(
            self.thread_id, live, self.bcast, self.db
        ):
            log.error(
                "CC %s refused cold transition while its prior guard remains",
                self.thread_id,
            )
            return None
        replacement = await self.resume()
        if replacement is None:
            return None
        if replacement.prepared_destination_identity is not destination.launch_identity:
            raise CCPreparedDestinationStale(
                "Claude Code replacement did not consume the admitted cold payload"
            )
        return replacement


def _stale_working_dir(transition: CCFeedTransition, live: _Live) -> tuple[bool, str | None]:
    live_wd = (
        getattr(live.session, "working_dir", None)
        if live.session is not None
        else None
    )
    if not (
        _proc_is_alive(live.session)
        and isinstance(live_wd, str)
        and live_wd
    ):
        return False, live_wd
    wd_db = transition.db or get_db()
    row = wd_db.execute(
        "SELECT project_id FROM threads WHERE id = ?", (transition.thread_id,)
    ).fetchone()
    project_id = row["project_id"] if row else None
    try:
        default = _main_working_dir(project_id, wd_db)
    except CCWorkingDirMissing:
        default = None
    return native_stale_working_dir(live_wd, default) is not None, live_wd


def _replacement_reason(
    transition: CCFeedTransition, live: _Live
) -> tuple[str | None, str | None]:
    if live.resume_rejected:
        return "rejected", None
    desired = transition.target.transport_model
    switching = live.session is not None and desired is not None and (
        live.model != desired
        or (
            transition.target.expected_reported_model is not None
            and live.expected_reported_model
            not in (None, transition.target.expected_reported_model)
        )
    )
    if switching:
        return "switching", None
    if live.provider_context_untrusted:
        return "untrusted", None
    stale, live_wd = _stale_working_dir(transition, live)
    if stale:
        return "stale_working_dir", live_wd
    if not _proc_is_alive(live.session):
        return "dead", live_wd
    return None, live_wd


def _log_replacement(
    transition: CCFeedTransition,
    live: _Live,
    reason: str,
    live_wd: str | None,
) -> None:
    if reason == "switching":
        log.info(
            "CC %s switching model %s → %s (respawn)",
            transition.thread_id,
            live.model,
            transition.target.transport_model,
        )
    elif reason == "untrusted":
        log.warning(
            "cc_runtime: %s provider-local context untrusted; "
            "respawning from CORSIA history",
            transition.thread_id,
        )
    elif reason == "stale_working_dir":
        log.warning(
            "cc_runtime: %s cwd %r is stale (outside current scope); "
            "respawning into a corrected directory [issue #841]",
            transition.thread_id,
            live_wd,
        )
    elif reason == "rejected":
        log.warning(
            "cc_runtime: %s resume was rejected before input; "
            "rebuilding from CORSIA history",
            transition.thread_id,
        )
    else:
        log.error(
            "cc_runtime: %s dead-in-_LIVE (returncode=%s); respawning via --resume",
            transition.thread_id,
            live.session.returncode if live.session else None,
        )


async def replace_if_required(
    transition: CCFeedTransition, live: _Live
) -> _Live | None:
    """Replace a rejected, switched, untrusted, stale-cwd, or dead process."""
    reason, live_wd = _replacement_reason(transition, live)
    if reason is None:
        return live
    _log_replacement(transition, live, reason, live_wd)
    if reason in {"switching", "untrusted", "stale_working_dir"}:
        retired = await close_and_finalize(
            transition.thread_id, live, transition.bcast, transition.db
        )
    else:
        retired = finalize(
            transition.thread_id, transition.bcast, transition.db
        ) is not None
    if not retired:
        log.error(
            "CC %s refused %s replacement while its guard remains",
            transition.thread_id,
            "dead-process" if reason in {"dead", "rejected"} else "live-process",
        )
        return None
    if reason == "rejected":
        # Claude refused this session before any input, so this input's own
        # target restarts from CORSIA history, never the missing session.
        await transition.retire_rejected_session(live)
        await transition.refresh_current_context()
    return await transition.resume()
