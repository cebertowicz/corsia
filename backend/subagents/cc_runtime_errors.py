from __future__ import annotations

import asyncio
import uuid
from typing import Callable

from infra.turn_errors import insert_turn_error
from infra.send_broadcast import broadcast_stored_message
from llm.error_cause import cause_fields


_EXPIRED_LOGIN_RECONCILIATION_TASKS: set[asyncio.Task] = set()


class CCLiveTurnBusy(RuntimeError):
    """A normal send reached Claude Code while its prior Turn still owns it."""


class CCPreparedDestinationStale(RuntimeError):
    """The provider boundary no longer matches the preflight payload."""


def is_resume_not_found(stderr: str) -> bool:
    detail = (stderr or "").lower()
    return (
        "no conversation found with session id" in detail
        or "no conversation found with session id:" in detail
    )


def is_empty_text_block_rejection(detail: str | None) -> bool:
    """Whether Anthropic rejected the replayed session over an empty text block.

    Claude Code persists an empty assistant text block in its own transcript
    and replays it on every resume, so this 400 repeats for as long as that
    session is resumed (#1138).
    """
    text = (detail or "").lower()
    return "400" in text and "text content blocks must be non-empty" in text


def reconcile_expired_login(detail: str | None, logger) -> None:
    """Schedule the canonical bounded check for Claude's exact expired-login error."""
    from connections.auth_failure import auth_failure_kind

    if auth_failure_kind("claude-code", detail) is None:
        return
    from connections.claude_discovery import reconcile_turn_auth_failure

    try:
        task = asyncio.get_running_loop().create_task(
            reconcile_turn_auth_failure(detail),
        )
    except RuntimeError:
        logger.warning(
            "Claude Code expired-login reconciliation skipped without a running loop",
        )
        return
    _EXPIRED_LOGIN_RECONCILIATION_TASKS.add(task)

    def report_failure(done: asyncio.Task) -> None:
        _EXPIRED_LOGIN_RECONCILIATION_TASKS.discard(done)
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.warning("Claude Code expired-login reconciliation failed: %s", type(error).__name__)

    task.add_done_callback(report_failure)


def surface_exit_error(
    thread_id: str,
    code: int | None,
    stderr: str,
    bcast,
    db,
    turn_identity,
    *,
    insert_message_fn: Callable,
    logger,
) -> str | None:
    if code in (0, None) and not (stderr or "").strip():
        return None
    last = next(
        (line for line in reversed((stderr or "").splitlines()) if line.strip()),
        f"Claude Code exited with code {code}.",
    )
    reconcile_expired_login(stderr, logger)
    text = f"⚠️ Claude Code stopped: {last.strip()[:300]}"
    try:
        meta = {
            "cc": True,
            "type": "cc_error",
            "event_type": "harness.error",
            "harness": "claude-code",
            **cause_fields("claude-code", "process_exited", stderr),
        }
        mid = insert_turn_error(
            insert_message_fn, thread_id, text, meta,
            turn_id=getattr(turn_identity, "turn_id", None),
            msg_id=str(uuid.uuid4()), db=db,
        )
        broadcast_stored_message(thread_id, mid, db=db, broadcast=bcast)
        if turn_identity is not None:
            bcast(thread_id, {
                "type": "turn_complete",
                "thread_id": thread_id,
                **turn_identity.frame_fields(),
            })
        return mid
    except Exception as exc:
        logger.error("cc_runtime: failed to surface CC exit error: %s", exc)
        return None
