"""Canonical Claude Code runtime composition surface."""

from subagents.cc_accept import accept_reconstructed_input as _accept_reconstructed_input
from subagents.cc_destination import prepare_cc_destination_payload
from subagents.cc_feed import ensure_live_then_feed, feed, feed_main
from subagents.cc_handle import (
    capture_retirement_owner,
    context_receipt_identity,
    live_handle_fingerprint as _cc_live_handle_fingerprint,
    retire_if_current,
    retarget_if_current,
    steer_restart_pending,
    trusted_handle_fingerprint,
)
from subagents.cc_preempt import preempt_active_turn as _preempt_active_turn
from subagents.cc_resume_transition import resume_same_turn_after_checkpoint
from subagents.cc_runtime_errors import (
    CCLiveTurnBusy,
    CCPreparedDestinationStale,
)
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_lifecycle import (
    emit_logical_close as _emit_logical_close,
    finalize as _finalize,
    finish_process_exit as _finish_process_exit,
    has_pending_or_live,
    is_live,
    retire,
    retire_live as _close_live,
    shutdown_all,
    stop,
)
from subagents.cc_runtime_state import _LIVE, _Live
from subagents.cc_spawn import spawn
from subagents.cc_runtime_steering import deliver_live_inbox_head
from subagents.child_thread import record_user_frame

__all__ = [
    "CCLiveTurnBusy",
    "CCPreparedDestinationStale",
    "capture_retirement_owner",
    "context_receipt_identity",
    "ensure_live_then_feed",
    "feed",
    "feed_main",
    "has_pending_or_live",
    "is_live",
    "prepare_cc_destination_payload",
    "record_user_frame",
    "retarget_if_current",
    "resume_same_turn_after_checkpoint",
    "retire",
    "retire_if_current",
    "deliver_live_inbox_head",
    "shutdown_all",
    "spawn",
    "stop",
    "steer_restart_pending",
    "trusted_handle_fingerprint",
]
