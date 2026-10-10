"""Runtime effects and quiescence at committed thread boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from infra.product_events import emit_product_event
from thread.model_state import ThreadQuiescenceError

if TYPE_CHECKING:
    from core.turn_core import TurnIdentity

_DESTRUCTIVE_QUIESCE_TIMEOUT_S = 10.0


def bump_history_epoch(db: Any, thread_id: str) -> int:
    """Advance transcript and runtime-context generations in one transaction."""
    row = db.execute(
        "UPDATE threads SET history_epoch = history_epoch + 1, "
        "runtime_context_epoch = runtime_context_epoch + 1 "
        "WHERE id = ? RETURNING history_epoch",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"thread does not exist: {thread_id}")
    return int(row[0])


async def retire_harness_continuations(
    thread_id: str,
    *,
    user_id: str | None = None,
    require_quiescent: bool = False,
    preserve_harness: str | None = None,
) -> dict[str, bool]:
    """Retire live/provider continuation state after context truth changes.

    Native turns rebuild from CORSIA's DB each time. Claude Code and Codex can
    keep provider-local/live state between turns, so Agent/scope/history/model
    boundaries must explicitly clear that state before the next accepted input.
    """
    from subagents import cc_runtime, codex_runtime
    from infra import state

    effects = {"cc_stopped": False, "codex_retired": False}
    retire_kwargs = {"broadcast": state.owner_broadcast(user_id)} if user_id else {}
    if preserve_harness != "claude-code":
        if cc_runtime.has_pending_or_live(thread_id):
            effects["cc_stopped"] = bool(
                await cc_runtime.retire(
                    thread_id,
                    **retire_kwargs,
                )
            )
        if require_quiescent and cc_runtime.has_pending_or_live(thread_id):
            raise RuntimeError(f"CC runtime on session {thread_id} remained live")
    if preserve_harness != "codex":
        effects["codex_retired"] = bool(
            await codex_runtime.retire(
                thread_id,
                **retire_kwargs,
            )
        )
        if require_quiescent and codex_runtime.has_pending_or_live(thread_id):
            raise RuntimeError(f"Codex runtime on session {thread_id} remained live")
    return effects


async def _quiesce_native_turn(
    thread_id: str,
    *,
    expected_turn: TurnIdentity | None = None,
) -> bool:
    """Cancel and join the exact tracked Native generation, when present."""
    from core.native_turn_control import cancel_and_join_native_turn
    from infra import state

    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return False
    try:
        identity = cancel_event.turn_identity
    except (AttributeError, RuntimeError) as exc:
        raise ThreadQuiescenceError(
            f"Native turn on session {thread_id} has no exact identity"
        ) from exc
    if expected_turn is not None and identity != expected_turn:
        raise ThreadQuiescenceError(
            f"Native turn owner changed before interruption on {thread_id}"
        )
    try:
        await cancel_and_join_native_turn(
            thread_id,
            timeout_s=_DESTRUCTIVE_QUIESCE_TIMEOUT_S,
            expected_turn=expected_turn,
        )
    except asyncio.TimeoutError as exc:
        raise ThreadQuiescenceError(
            f"Native turn {identity.turn_id} did not stop before the boundary"
        ) from exc
    except Exception as exc:
        raise ThreadQuiescenceError(
            f"Native turn {identity.turn_id} did not stop cleanly"
        ) from exc

    if state._active_turns.get(thread_id) is cancel_event:
        raise ThreadQuiescenceError(
            f"Native turn {identity.turn_id} retained its runtime owner"
        )
    return True


async def quiesce_exact_turn_owner(
    thread_id: str,
    *,
    expected_turn: TurnIdentity,
    user_id: str | None,
) -> dict[str, bool]:
    """Stop only the runtimes still bound to one exact durable Turn."""
    from infra import state
    from infra.database import get_db
    from subagents import cc_runtime, codex_runtime

    broadcast = state.owner_broadcast(user_id) if user_id else None
    try:
        native_cancelled = await _quiesce_native_turn(
            thread_id,
            expected_turn=expected_turn,
        )
        cc_stopped = await cc_runtime.stop(
            thread_id,
            expected_turn_id=expected_turn.turn_id,
            broadcast=broadcast,
        )
        codex_stopped = await codex_runtime.stop(
            thread_id,
            expected_turn_id=expected_turn.turn_id,
            broadcast=broadcast,
        )
    except ThreadQuiescenceError:
        raise
    except Exception as exc:
        raise ThreadQuiescenceError(
            f"Turn {expected_turn.turn_id} did not stop cleanly"
        ) from exc

    db = get_db()
    try:
        row = db.execute(
            "SELECT thread_id,status,turn_order FROM active_turns WHERE id=?",
            (expected_turn.turn_id,),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise ThreadQuiescenceError(
            f"Turn {expected_turn.turn_id} disappeared during interruption"
        )
    if (
        str(row["thread_id"]) != thread_id
        or int(row["turn_order"]) != expected_turn.turn_order
    ):
        raise ThreadQuiescenceError(
            f"Turn {expected_turn.turn_id} identity changed during interruption"
        )
    if str(row["status"]) == "running":
        raise ThreadQuiescenceError(
            f"Turn {expected_turn.turn_id} remained active after interruption"
        )
    return {
        "native_cancelled": native_cancelled,
        "cc_stopped": bool(cc_stopped),
        "codex_stopped": bool(codex_stopped),
    }


async def quiesce_thread_runtimes(
    thread_id: str,
    *,
    user_id: str | None,
    preserve_harness: str | None = None,
) -> dict[str, bool]:
    """Prove Native and every non-preserved harness runtime quiescent."""
    native_cancelled = await _quiesce_native_turn(thread_id)
    try:
        effects = await retire_harness_continuations(
            thread_id,
            user_id=user_id,
            require_quiescent=True,
            preserve_harness=preserve_harness,
        )
    except Exception as exc:
        raise ThreadQuiescenceError(
            f"Provider runtime on session {thread_id} did not stop cleanly"
        ) from exc
    return {"native_cancelled": native_cancelled, **effects}


def emit_context_boundary_changed(
    thread_id: str,
    *,
    user_id: str | None,
    reason: str,
    effects: dict[str, bool],
) -> None:
    """Emit a context boundary whose runtime effects are already complete."""
    emit_product_event(
        "context",
        "boundary_changed",
        thread_id=thread_id,
        user_id=user_id,
        properties={"reason": reason, **effects},
    )


async def post_context_boundary_effects(
    thread_id: str,
    *,
    user_id: str | None,
    reason: str,
) -> None:
    """Side effects after a committed Agent/scope/history context boundary."""
    effects = await retire_harness_continuations(
        thread_id,
        user_id=user_id,
    )
    emit_context_boundary_changed(
        thread_id,
        user_id=user_id,
        reason=reason,
        effects=effects,
    )
