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
    """The model cache captured for this account changed before admission."""

    def __init__(self):
        super().__init__("account_model_cache_changed")


def cache_error_marker(exc, thread_id, identity, bcast, db, insert_message_fn, logger):
    if not isinstance(exc, CodexModelCacheChanged):
        return None

    async def marker():
        from connections.account_profiles import account_label
        from connections.vendor_accounts import get_account
        from infra.db_async import run_in_db_unless_owned
        from connections.account_runtime import thread_launch_homes
        from infra.database import get_db

        def write_marker():
            connection = db or get_db()
            try:
                homes = thread_launch_homes(thread_id, "codex", turn_id=identity.turn_id, db=connection)
                account = get_account(homes.account_id, db=connection) if homes.account_id else None
                name = account_label(account, db=connection)["text"] if account else "this account"
                text = (f"Couldn’t start Codex on {name}: its model list is out of date. "
                        f"Use Sign in again on {name} under Settings › Providers.")
                meta = {"codex": True, "type": "cc_error", "event_type": "harness.error",
                        "harness": "codex", "cause": "account_model_cache_changed"}
                if account:
                    meta["account_id"] = account["id"]
                mid = insert_turn_error(insert_message_fn, thread_id, text, meta,
                                        turn_id=identity.turn_id, msg_id=str(uuid.uuid4()), db=connection)
                broadcast_stored_message(thread_id, mid, db=connection, broadcast=bcast)
                return mid
            finally:
                if db is None:
                    connection.close()

        try:
            return await run_in_db_unless_owned(db, write_marker)
        except Exception as exc:
            logger.error("codex_runtime: failed to surface cache error: %s", exc)
            return None

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
) -> str | None:
    if code == 0 and not terminal_applied:
        return None
    if code is None and not (stderr or "").strip():
        return None
    last = next(
        (line for line in reversed((stderr or "").splitlines()) if line.strip()),
        f"Codex exited with code {code}.",
    )
    text = f"⚠️ Codex stopped: {last.strip()[:300]}"
    try:
        meta = {
            "codex": True,
            "type": "cc_error",
            "event_type": "harness.error",
            "harness": "codex",
            **cause_fields("codex", "process_exited", stderr),
        }
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
