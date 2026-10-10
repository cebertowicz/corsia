from __future__ import annotations

import uuid
from typing import Callable

from infra.send_broadcast import broadcast_stored_message


class CodexLiveTurnBusy(RuntimeError):
    """A normal send reached Codex while its prior Turn still owns the handle."""


class CodexPreparedContextStale(RuntimeError):
    """Raised before provider write when Codex admission is no longer exact."""


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
            "cause": "process_exited",
        }
        mid = insert_message_fn(
            thread_id, "system", text, meta,
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
