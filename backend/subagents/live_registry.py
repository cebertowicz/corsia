"""Per-session provider handles and transition locks.

Locks cover state transitions and input acceptance, never a streaming turn.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar

from core.turn_core import TurnIdentity


log = logging.getLogger(__name__)

#: How long the user's Stop gives runtime owners to end the exact Turn.
STOP_LOCK_TIMEOUT_S: float = 30.0


class StaleTransition(RuntimeError):
    """A transition used an abandoned lock generation."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"transition lock generation for {thread_id} was abandoned")


class _Generation(asyncio.Lock):
    """One session's transition lock; its waiters fail fenced once abandoned."""

    def __init__(self, generations: dict[str, asyncio.Lock], thread_id: str) -> None:
        super().__init__()
        self._generations = generations
        self._thread_id = thread_id

    def _current(self) -> bool:
        return self._generations.get(self._thread_id) is self

    async def acquire(self) -> Literal[True]:
        """Acquire, then prove this is still the session's current generation.

        A waiter that ``abandon`` woke passes the lock to the next waiter and
        raises ``StaleTransition`` without entering its transition.
        """
        await super().acquire()
        if not self._current():
            super().release()
            raise StaleTransition(self._thread_id)
        return True

    def release(self) -> None:
        """Release; once abandoned, the stalled holder's late exit is a no-op.

        ``abandon`` already released that generation for its holder.
        """
        if self.locked() or self._current():
            super().release()


async def acquire_stop_lock(lock: asyncio.Lock, *, thread_id: str, runtime: str) -> bool:
    """Wait a bounded time for one transition lock on the Stop path.

    False means the holder did not release within the bound, or its
    generation was abandoned meanwhile, so the runtime did not end the exact
    Turn itself. Stop still ends it: ``do_stop`` commits the durable cancel,
    and its retirement pass abandons the stalled lock generation. The next
    ``lock()`` is then fresh, every waiter on the stalled generation fails
    fenced, and ``publish`` refuses the stalled holder's generation, so it can
    neither block the successor nor republish a handle for the settled Turn.
    A holder that stalls is still a defect to fix at its source (2026-09-16
    re-entry, #1793; audit #1797).
    """
    try:
        async with asyncio.timeout(STOP_LOCK_TIMEOUT_S):
            await lock.acquire()
    except (TimeoutError, StaleTransition):
        log.error(
            "%s.stop %s: transition lock held >%ss; Stop ends the Turn without it",
            runtime, thread_id, STOP_LOCK_TIMEOUT_S,
        )
        return False
    return True


def _new_checkpoint_wait_scope():
    from core.checkpoint_resume import CheckpointWaitScope

    return CheckpointWaitScope()


@dataclass
class LiveBase:
    """State shared by provider-specific live handles."""

    session: object | None
    # Where this handle addresses its parent for broadcasts. For a delegated
    # child that is ``threads.spawned_by``, not the same-named lineage column;
    # it is never the thread's role (issue #1617).
    parent_thread_id: str | None
    name: str
    thinking_buf: str = ""
    tool_names: dict = field(default_factory=dict)
    # Provider tool-call id -> durable Assistant-Step row id. Entries live only
    # until the canonical role=tool result commits.
    tool_step_ids: dict[str, str] = field(default_factory=dict)
    last_text: str = ""
    last_assistant_message_id: str | None = None
    turn_has_assistant_row: bool = False
    dropped_line_this_turn: bool = False
    corsia_turn: TurnIdentity | None = None
    context_usage_stale_reason: str | None = None
    # Only a synchronously-created hard-wall checkpoint is cancellable by the
    # exact active Turn. Background/manual work never enters this scope.
    checkpoint_wait_scope: object = field(
        default_factory=_new_checkpoint_wait_scope,
        repr=False,
    )


H = TypeVar("H", bound=LiveBase)


class LiveRegistry(Generic[H]):
    """``thread_id`` to live handle, with one transition lock per thread."""

    def __init__(self) -> None:
        self._live: dict[str, H] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, thread_id: str) -> asyncio.Lock:
        """The per-session transition lock (created on first use)."""
        return self._locks.setdefault(thread_id, _Generation(self._locks, thread_id))

    def existing_lock(self, thread_id: str) -> asyncio.Lock | None:
        """Return an existing transition lock without creating one."""
        return self._locks.get(thread_id)

    def get(self, thread_id: str, default: H | None = None) -> H | None:
        """The live handle for a session, or ``default`` if not live."""
        return self._live.get(thread_id, default)

    def __contains__(self, thread_id: str) -> bool:
        return thread_id in self._live

    def put(self, thread_id: str, handle: H) -> None:
        """Track a session as live, for isolated test fixtures only.

        Production code publishes through ``publish`` so an abandoned lock
        generation can never register a handle.
        """
        self._live[thread_id] = handle

    def holds(self, thread_id: str, held: asyncio.Lock) -> bool:
        """Whether ``held`` is the session's current, acquired lock generation."""
        return held is self._locks.get(thread_id) and held.locked()

    def require_held(self, thread_id: str, held: asyncio.Lock) -> None:
        """Raise unless ``held`` is the session's current, acquired generation."""
        if not self.holds(thread_id, held):
            raise StaleTransition(thread_id)

    def publish(self, thread_id: str, handle: H, *, held: asyncio.Lock) -> None:
        """Track a live handle for the holder of the current lock generation."""
        self.require_held(thread_id, held)
        self._live[thread_id] = handle

    def abandon(self, thread_id: str, turn_id: str) -> H | None:
        """Retire a stalled lock generation and detach one Turn's handle.

        Only Stop's retirement pass calls this, after the Turn is durably
        terminal and the pass deadline has expired. A held lock is dropped and
        released for its stalled holder, so the next ``lock()`` is a fresh
        generation and every task already waiting on the old one wakes and
        fails fenced. The handle still bound to ``turn_id`` is unregistered and
        returned; the caller releases its checkpoint wait and terminates its
        process.
        """
        lock = self._locks.get(thread_id)
        if lock is not None and lock.locked():
            del self._locks[thread_id]
            lock.release()
        live = self._live.get(thread_id)
        identity = None if live is None else live.corsia_turn
        if live is None or identity is None or identity.turn_id != turn_id:
            return None
        del self._live[thread_id]
        return live

    def pop(self, thread_id: str, default: H | None = None) -> H | None:
        """Stop tracking a session; returns the handle (or ``default``)."""
        return self._live.pop(thread_id, default)

    def count(self) -> int:
        """Return the number of tracked live handles."""
        return len(self._live)

    def ids(self) -> tuple[str, ...]:
        """Return a stable snapshot of tracked session ids."""
        return tuple(self._live)

    def reset(self) -> None:
        """Clear live handles and transition locks for isolated test fixtures."""
        self._live.clear()
        self._locks.clear()
