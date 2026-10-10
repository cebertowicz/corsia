"""Process-local control for the exact active Native turn."""

from __future__ import annotations

import asyncio

from core.turn_core import TurnIdentity as _TurnIdentity
from infra import state


class TurnCancelEvent(asyncio.Event):
    """Exact Native cancellation handle with replacement and join state."""

    def __init__(self) -> None:
        super().__init__()
        from core.checkpoint_resume import CheckpointWaitScope

        self.replacement_requested = False
        self.user_stop_requested = False
        self.restart_boundary_stopped = False
        self.completion: asyncio.Future[bool] = (
            asyncio.get_running_loop().create_future()
        )
        self.completion.add_done_callback(_consume_completion_exception)
        self._turn_identity: _TurnIdentity | None = None
        self.checkpoint_wait_scope = CheckpointWaitScope()

    def bind_turn_identity(self, identity: _TurnIdentity) -> None:
        """Bind this cancellation handle to one durable generation exactly once."""
        if self._turn_identity is not None and self._turn_identity != identity:
            raise RuntimeError("native cancellation handle identity already bound")
        self._turn_identity = identity

    @property
    def turn_identity(self) -> _TurnIdentity:
        if self._turn_identity is None:
            raise RuntimeError("native cancellation handle identity is not bound")
        return self._turn_identity


def request_native_restart(
    thread_id: str,
    expected_turn_id: str,
    message: str,
) -> bool:
    """Use the exact cancellation handle to request one restart boundary."""
    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return False
    identity = _require_native_turn_identity(thread_id, cancel_event)
    if identity.turn_id != expected_turn_id:
        return False
    setattr(cancel_event, "restart_pending", True)
    setattr(cancel_event, "restart_pending_message", message)
    setattr(cancel_event, "restart_cancelled_message", None)
    setattr(cancel_event, "restart_boundary_stopped", False)
    return True


def native_restart_boundary_stopped(
    thread_id: str,
    expected_turn_id: str,
) -> bool:
    """Return whether this exact Native Turn committed to stopping."""
    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return False
    identity = _require_native_turn_identity(thread_id, cancel_event)
    if identity.turn_id != expected_turn_id:
        return False
    return bool(getattr(cancel_event, "restart_boundary_stopped", False))


def cancel_native_restart(
    thread_id: str,
    expected_turn_id: str,
    message: str,
) -> bool:
    """Compensate restart control through the same exact cancellation handle."""
    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return False
    identity = _require_native_turn_identity(thread_id, cancel_event)
    if identity.turn_id != expected_turn_id:
        return False
    setattr(cancel_event, "restart_pending", False)
    setattr(cancel_event, "restart_pending_message", None)
    setattr(cancel_event, "restart_cancelled_message", message)
    return True


def _require_native_turn_identity(
    thread_id: str,
    cancel_event: object,
) -> _TurnIdentity:
    """Return the exact durable identity bound to one active Native handle."""
    try:
        identity = cancel_event.turn_identity  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError) as exc:
        raise RuntimeError(
            f"active turn on session {thread_id} has no exact turn identity"
        ) from exc
    if not isinstance(identity, _TurnIdentity):
        raise RuntimeError(
            f"active turn on session {thread_id} has no exact turn identity"
        )
    return identity


async def _terminate_native_turn_processes(
    thread_id: str,
    identity: _TurnIdentity,
) -> None:
    """Stop only foreground groups owned by the exact durable Native turn."""
    from host_access.processes import PROCESS_OWNER

    await PROCESS_OWNER.terminate_turn(
        thread_id,
        identity.turn_id,
        foreground_only=True,
        reason="native_turn_interrupted",
    )


async def cancel_and_join_native_turn(
    thread_id: str,
    *,
    timeout_s: float = 10.0,
    expected_turn: _TurnIdentity | None = None,
    replacement_requested: bool = True,
) -> bool:
    """Signal and join the exact tracked Native generation for a session."""
    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return False
    identity = _require_native_turn_identity(thread_id, cancel_event)
    if expected_turn is not None and identity != expected_turn:
        raise RuntimeError(
            f"active turn owner changed before interruption on {thread_id}"
        )
    llm_task = state._active_llm_tasks.get(thread_id)
    completion = getattr(cancel_event, "completion", None)
    join_target = completion or state._active_turn_tasks.get(thread_id)

    setattr(cancel_event, "replacement_requested", replacement_requested)
    try:
        if not replacement_requested:
            cancel_event.checkpoint_wait_scope.request_stop()
        cancel_event.set()
        if (
            llm_task is not None
            and state._active_llm_tasks.get(thread_id) is llm_task
        ):
            llm_task.cancel()
        await _terminate_native_turn_processes(thread_id, identity)
        if join_target is None:
            raise RuntimeError(
                f"active turn on session {thread_id} has no join handle"
            )
        try:
            await asyncio.wait_for(asyncio.shield(join_target), timeout=timeout_s)
        except asyncio.CancelledError as exc:
            if join_target.done():
                raise RuntimeError(
                    f"active turn on session {thread_id} did not stop cleanly"
                ) from exc
            raise
    finally:
        setattr(cancel_event, "replacement_requested", False)
    return True


def _consume_completion_exception(completion: asyncio.Future) -> None:
    """Retrieve unobserved join failures without hiding them from awaiters."""
    if completion.cancelled():
        return
    completion.exception()
