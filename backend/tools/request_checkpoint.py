"""Agent-requested checkpoint admission with one observable outcome per call."""

from __future__ import annotations

from typing import Any

from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS
from infra.db_async import run_in_db
from tools.base import Tool


def _caller_user_id(thread_id: str) -> str | None:
    """Resolve the tool caller's user from its backend-owned thread identity."""
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        return str(row["user_id"]) if row is not None and row["user_id"] else None
    finally:
        db.close()


def _emit(
    thread_id: str,
    outcome: str,
    reason_key: str,
    request_reason: str,
    **properties: object,
) -> None:
    """Write the one event and server-log line required for a tool decision."""
    from infra.product_events import emit_product_event
    from loguru import logger

    payload: dict[str, object] = {
        "reason": reason_key,
        "request_reason": request_reason,
        **properties,
    }
    logger.info(
        "agent_checkpoint {}: thread={} reason={} request_reason={!r}",
        outcome, thread_id, reason_key, request_reason,
    )
    emit_product_event("agent_checkpoint", outcome, thread_id=thread_id, properties=payload)


def _refusal_message(reason_key: str, details: dict[str, object]) -> str:
    """Give agents one actionable response for each admission refusal."""
    if reason_key == "below_minimum":
        return (
            "Not enough new material to be worth a paid fold "
            f"({details.get('foldable_tokens', 0)} of "
            f"{details.get('threshold_tokens', 32_768):,} tokens since the last "
            "checkpoint). Keep working; automatic checkpoints still apply."
        )
    if reason_key in {"already_covered", "no_new_history", "no_sealed_boundary"}:
        return "Nothing new is sealed since the last checkpoint. Finish a turn first."
    if reason_key == "coalesced":
        return "A checkpoint is already in progress for this thread."
    if reason_key in {"target_unavailable", "model_unavailable"}:
        return "This thread's model is unavailable, so a checkpoint cannot run now."
    if reason_key == "warm_capture_refused":
        return "The provider session cannot be folded right now. Try after your next turn."
    return "Checkpoint is not available right now. Continue working and try after a later boundary."


class RequestCheckpointTool(Tool):
    """Request a bounded checkpoint at an agent-chosen work boundary."""

    @property
    def name(self) -> str:
        return "request_checkpoint"

    @property
    def description(self) -> str:
        return (
            "Request a checkpoint (context fold) at a boundary you choose. "
            f"Consider this after finishing a plan node or phase with at least "
            f"{IDLE_MIN_DELTA_TOKENS:,} new tokens since the last checkpoint, "
            "when the next work is unrelated. Do not call it after small steps or "
            "repeatedly. This only requests the fold: it "
            "installs on its own, needs no follow-up, and does not stop your work. "
            "Claude Code and Codex fold through the last completed turn; Native "
            "folds through the latest completed assistant step, so record anything "
            "essential with continuity_edit first. Refusals are normal: read the "
            "reason and continue. To read earlier checkpoint conclusions, use "
            "get_checkpoints instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Why this is a good boundary, at most 200 characters. "
                        "Audit only; it does not influence the checkpoint summary."
                    ),
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        }

    async def execute(self, reason: str, **kwargs: Any) -> str:
        """Prepare, start, or coalesce one agent checkpoint for the current thread."""
        from core.agent_checkpoint_runtime import prepare_native_agent_checkpoint
        from core.checkpoint_agent_admission import (
            CheckpointNotReady,
            prepare_external_checkpoint,
        )
        from core.checkpoint_runtime import CheckpointTargetLimited, request_checkpoint

        thread_id = kwargs.get("_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: request_checkpoint could not resolve the calling thread."
        request_reason = reason.strip()
        if not request_reason:
            return "Error: reason must contain at least one non-whitespace character."
        try:
            prepared = await prepare_native_agent_checkpoint()
            native = prepared is not None
            if prepared is None:
                user_id = await run_in_db(_caller_user_id, thread_id)
                if user_id is None:
                    return "Error: request_checkpoint could not authorize the calling thread."
                prepared = await prepare_external_checkpoint(
                    thread_id, user_id, trigger="agent",
                )
            if prepared is None:
                _emit(thread_id, "refused", "no_sealed_boundary", request_reason)
                return _refusal_message("no_sealed_boundary", {})
            if isinstance(prepared, tuple):
                checkpoint_request, provider_request = prepared
            else:
                checkpoint_request = prepared.request
                provider_request = prepared.provider_request
            record = await request_checkpoint(checkpoint_request, provider_request)
            if record.id != checkpoint_request.id:
                _emit(thread_id, "refused", "coalesced", request_reason)
                return _refusal_message("coalesced", {})
            through_seq = record.through_seq
            _emit(
                thread_id,
                "started",
                "fired",
                request_reason,
                through_seq=through_seq,
                writer_harness=record.writer_harness,
            )
            boundary = "latest completed assistant step" if native else "last completed turn"
            return (
                "Checkpoint requested. It folds history through the "
                f"{boundary} (about the required 32,768+ new tokens). Your current "
                "turn's most recent work may not be included. Continue as normal; "
                "the fold installs on its own."
            )
        except CheckpointTargetLimited:
            _emit(thread_id, "refused", "target_limited", request_reason)
            return _refusal_message("target_limited", {})
        except CheckpointNotReady as error:
            _emit(thread_id, "refused", error.reason_key, request_reason, **error.details)
            return _refusal_message(error.reason_key, error.details)
        except Exception as error:
            _emit(thread_id, "failed", "failed", request_reason, error=type(error).__name__)
            return "Checkpoint request failed. Continue working; the server recorded the failure."
