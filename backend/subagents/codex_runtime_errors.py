from __future__ import annotations

import uuid
from typing import Callable

from infra.turn_errors import insert_turn_error
from infra.send_broadcast import broadcast_stored_message
from llm.error_cause import cause_fields


class CodexLiveTurnBusy(RuntimeError):
    """A normal send reached Codex while its prior Turn still owns the handle."""


def is_live_writer_refusal(error: BaseException | None) -> bool:
    """Whether ``error`` means a prior Codex writer still owns the thread."""
    from subagents.codex_app_server_errors import CodexAppServerError

    return isinstance(error, CodexLiveTurnBusy) or (
        isinstance(error, CodexAppServerError) and error.is_active_writer
    )


class CodexPreparedContextStale(RuntimeError):
    """Raised before provider write when Codex admission is no longer exact."""


class CodexModelCacheChanged(RuntimeError):
    def __init__(self, account_id: str | None, message: str):
        super().__init__(message)
        self.account_id = account_id


def explain_model_cache_change(turn_identity, logger, *, db=None) -> CodexModelCacheChanged:
    from infra.failure_reference import new_reference
    from subagents.turn_account_error import turn_account

    account = turn_account(getattr(turn_identity, "turn_id", None), "codex", db=db)
    name = account["name"] if account else "this account"
    ref = new_reference("local")
    logger.warning("Codex launch failed reference=%s cause=account_model_cache_changed", ref)
    return CodexModelCacheChanged(
        account["id"] if account else None,
        f"Couldn’t start Codex on {name}: its model list is out of date. Reference: {ref}",
    )


def cache_error_marker(exc, thread_id, live, bcast, db, insert_message_fn, logger):
    if not isinstance(exc, CodexModelCacheChanged):
        return None

    async def marker():
        return surface_exit_error(
            thread_id, 1, str(exc), bcast, db, live.corsia_turn,
            terminal_applied=True, emit_terminal=False, insert_message_fn=insert_message_fn,
            logger=logger, account_id=exc.account_id, formatted=True,
        )
    return marker


def surface_exit_error(
    thread_id: str,
    code: int | None,
    stderr: str,
    bcast,
    db,
    turn_identity,
    *,
    terminal_applied: bool = False,
    emit_terminal: bool = True,
    insert_message_fn: Callable,
    logger,
    account_id: str | None = None,
    formatted: bool = False,
) -> str | None:
    if code == 0 and not terminal_applied:
        return None
    if code is None and not (stderr or "").strip():
        return None
    last = next(
        (line for line in reversed((stderr or "").splitlines()) if line.strip()),
        f"Codex exited with code {code}.",
    )
    text = stderr if formatted else f"⚠️ Codex stopped: {last.strip()[:300]}"
    try:
        meta = {
            "codex": True,
            "type": "cc_error",
            "event_type": "harness.error",
            "harness": "codex",
            **cause_fields("codex", "process_exited", stderr),
        }
        if account_id:
            meta["account_id"] = account_id
        mid = insert_turn_error(
            insert_message_fn, thread_id, text, meta,
            turn_id=getattr(turn_identity, "turn_id", None),
            msg_id=str(uuid.uuid4()), db=db,
        )
        broadcast_stored_message(thread_id, mid, db=db, broadcast=bcast)
        if emit_terminal:
            bcast(thread_id, {
                "type": "turn_complete",
                "thread_id": thread_id,
                **turn_identity.frame_fields(),
            })
        return mid
    except Exception as exc:
        logger.error("codex_runtime: failed to surface exit error: %s", exc)
        return None
