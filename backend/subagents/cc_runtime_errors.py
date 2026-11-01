from __future__ import annotations

import uuid
from typing import Callable

from infra.turn_errors import insert_turn_error
from infra.send_broadcast import broadcast_stored_message
from llm.error_cause import cause_fields


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
    account=None,
    emit_terminal: bool = True,
) -> str | None:
    if code in (0, None) and not (stderr or "").strip():
        return None
    last = next(
        (line for line in reversed((stderr or "").splitlines()) if line.strip()),
        f"Claude Code exited with code {code}.",
    )
    text = f"⚠️ Claude Code stopped: {last.strip()[:300]}"
    if account and account["state"] == "signed_out":
        from connections.account_profiles import account_label

        label = account_label(account, db=db)["text"]
        text = f'{label} is signed out of Claude Code. Sign it in again under Settings › Providers.'
    try:
        meta = {
            "cc": True,
            "type": "cc_error",
            "event_type": "harness.error",
            "harness": "claude-code",
            **cause_fields("claude-code", "process_exited", stderr),
        }
        if account and account["state"] == "signed_out":
            meta["account_id"] = account["id"]
        mid = insert_turn_error(
            insert_message_fn, thread_id, text, meta,
            turn_id=getattr(turn_identity, "turn_id", None),
            msg_id=str(uuid.uuid4()), db=db,
        )
        broadcast_stored_message(thread_id, mid, db=db, broadcast=bcast)
        if emit_terminal and turn_identity is not None:
            bcast(thread_id, {
                "type": "turn_complete",
                "thread_id": thread_id,
                **turn_identity.frame_fields(),
            })
        return mid
    except Exception as exc:
        logger.error("cc_runtime: failed to surface CC exit error: %s", exc)
        return None
