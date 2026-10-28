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
    """The account's model cache changed before provider admission."""

    def __init__(self) -> None:
        super().__init__("account_model_cache_changed")


def cache_error_marker(exc, thread_id, live, bcast, db, insert_message_fn, logger):
    if not isinstance(exc, CodexModelCacheChanged):
        return None

    async def marker():
        from infra.db_async import run_in_db_unless_owned
        from infra.failure_reference import new_reference
        from subagents.turn_account_error import turn_account

        account = await run_in_db_unless_owned(
            db, turn_account, getattr(live.corsia_turn, "turn_id", None), "codex", db=db,
        )
        name = account["name"] if account else "this account"
        ref = new_reference("local")
        logger.warning("Codex launch failed reference=%s cause=account_model_cache_changed", ref)
        return surface_exit_error(
            thread_id, 1, f"Couldn’t start Codex on {name}: its model list is out of date. Reference: {ref}",
            bcast, db, live.corsia_turn, terminal_applied=True, emit_terminal=False,
            insert_message_fn=insert_message_fn, logger=logger,
            account_id=account["id"] if account else None, formatted=True, ref=ref,
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
    ref: str | None = None,
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
        if ref:
            meta["ref"] = ref
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
