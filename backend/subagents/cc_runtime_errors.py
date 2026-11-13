from __future__ import annotations

import uuid
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable

from infra.turn_errors import insert_turn_error
from infra.send_broadcast import broadcast_stored_message
from llm.error_cause import cause_fields

if TYPE_CHECKING:
    from core.turn_core import TerminalSettlement, TurnIdentity
    from subagents.cc_runtime_state import _Live


class CCLiveTurnBusy(RuntimeError):
    """A normal send reached Claude Code while its prior Turn still owns it."""


class CCPreparedDestinationStale(RuntimeError):
    """The provider boundary no longer matches the preflight payload."""


class ResumeVerdictOwnsWrite(Exception):
    """Claude closed stdin before a native resume's frame drained.

    The feed's pending resume verdict decides that write: a refusal proves
    Claude never read it, so the feed redelivers it; any other verdict runs
    ``commit_unverified``, the ordinary failed-drain terminal.
    """

    def __init__(
        self,
        identity: TurnIdentity,
        error: BaseException,
        commit_unverified: Callable[[], Awaitable[TerminalSettlement]],
    ) -> None:
        super().__init__(str(error))
        self.identity = identity
        self.error = error
        self.commit_unverified = commit_unverified

    @staticmethod
    def applies(live: _Live, accepted: bool, error: BaseException) -> bool:
        """An unaccepted write lost to a closed pipe while a resume verdict is pending."""
        return (
            not accepted
            and live.resume_verdict is not None
            and isinstance(error, (BrokenPipeError, ConnectionResetError))
        )


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
    login_answered: bool = False,
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
            **cause_fields("claude-code", "process_exited", stderr, login_answered=login_answered),
        }
        if account and account["state"] == "signed_out":
            meta["account_id"] = account["id"]
        mid = insert_turn_error(
            insert_message_fn, thread_id, text, meta,
            turn_id=getattr(turn_identity, "turn_id", None),
            msg_id=str(uuid.uuid4()), db=db,
        )
        broadcast_stored_message(thread_id, mid, db=db, broadcast=bcast)
        return mid
    except Exception as exc:
        logger.error("cc_runtime: failed to surface CC exit error: %s", exc)
        return None
