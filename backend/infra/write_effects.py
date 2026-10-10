"""Effects a write transaction may only perform once it has durably committed.

Effects are held per connection. A SAVEPOINT rollback drops the effects
registered inside it and an outermost rollback drops them all, so a rolled-back
write never leaves an in-memory or analytics side behind. ``write_transaction``
owns the boundaries; callers only register through ``after_commit``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

log = logging.getLogger(__name__)

Effect = Callable[[], None]

_pending: dict[int, list[Effect]] = {}
_open_scopes: set[int] = set()  # connections inside an outermost write_transaction
_frames: dict[int, int] = {}  # open nested write_transaction frames per connection
_marks: dict[int, list[int]] = {}  # effect-queue length at each open nested frame's start
_foreign: set[int] = set()  # connections whose frames nest in a transaction nobody here owns


def connection_key(connection: sqlite3.Connection) -> int:
    """Return the identity of the raw connection behind a pooled wrapper."""
    return id(getattr(connection, "_conn", connection))


def begin(connection: sqlite3.Connection) -> None:
    """``write_transaction`` opened the outermost transaction on ``connection``."""
    _open_scopes.add(connection_key(connection))
    _pending.pop(connection_key(connection), None)


def after_commit(connection: sqlite3.Connection, effect: Effect) -> None:
    """Run ``effect`` once the write transaction open on ``connection`` commits.

    Outside any transaction there is nothing to wait for, so it runs at once.
    Called inside a transaction with no ``write_transaction`` frame open (Python's
    implicit transaction, or a raw BEGIN), or inside a frame nested in one, nothing
    would ever report that transaction's commit, so it raises rather than lose the
    effect silently or run it before the real commit.
    """
    if not connection.in_transaction:
        run([effect])
        return
    key = connection_key(connection)
    if key in _foreign or (key not in _open_scopes and not _frames.get(key)):
        from infra.database import TransactionOwnershipError

        raise TransactionOwnershipError(
            "after_commit needs a write_transaction scope: this connection's open "
            "transaction is not owned by one, so nothing would run the effect"
        )
    _pending.setdefault(key, []).append(effect)


def after_commit_merged(connection: sqlite3.Connection, key: object, items: list,
                        effect: Callable[[list], None]) -> None:
    """Run ``effect`` once after commit with every item registered under ``key`` in this transaction.

    Repeated registrations extend the pending call (first-seen order, no
    duplicates) instead of queueing another. Items added inside a savepoint
    that rolls back stay merged, so ``effect`` must tolerate a stale item.
    """
    queued = next((pending for pending in _pending.get(connection_key(connection), ())
                   if isinstance(pending, _Merged) and pending.key == key), None) if connection.in_transaction else None
    if queued is not None:
        queued.items.extend(item for item in items if item not in queued.items)
        return
    after_commit(connection, _Merged(key, items, effect))


class _Merged:
    """A pending effect that runs once with every item registered under its key."""

    def __init__(self, key: object, items: list, effect: Callable[[list], None]) -> None:
        self.key, self.items, self.effect = key, list(dict.fromkeys(items)), effect

    def __call__(self) -> None:
        """Run the effect with the merged items."""
        self.effect(self.items)


def pending(connection: sqlite3.Connection) -> list[Effect]:
    """The effects registered in the innermost open frame of ``connection``'s transaction.

    A rollback of that frame drops exactly these, so a caller may amend them.
    """
    key = connection_key(connection)
    marks = _marks.get(key)
    return _pending.get(key, [])[marks[-1] if marks else 0:]


def enter(connection: sqlite3.Connection) -> int:
    """A nested ``write_transaction`` frame (SAVEPOINT) opened; return its mark."""
    key = connection_key(connection)
    if key not in _open_scopes and not _frames.get(key):
        _foreign.add(key)
    _frames[key] = _frames.get(key, 0) + 1
    mark = len(_pending.setdefault(key, []))
    _marks.setdefault(key, []).append(mark)
    return mark


def leave(connection: sqlite3.Connection, since: int, *, ok: bool) -> list[Effect]:
    """A nested frame ended; return the effects due now.

    A failed frame drops what it registered. A frame nested in an outermost
    ``write_transaction`` keeps its effects for that commit. A frame nested in a
    transaction some other owner controls has no commit to wait for, so its
    effects are due at its own release.
    """
    key = connection_key(connection)
    _frames[key] -= 1
    _marks[key].pop()
    if not _frames[key]:
        del _frames[key]
        del _marks[key]
        _foreign.discard(key)
    queue = _pending.get(key, [])
    tail = queue[since:]
    if ok and key in _open_scopes:
        return []
    del queue[since:]
    return tail if ok else []


def take(connection: sqlite3.Connection) -> list[Effect]:
    """The outermost transaction committed: close its scope, hand back its effects."""
    _open_scopes.discard(connection_key(connection))
    return _pending.pop(connection_key(connection), [])


def abort(connection: sqlite3.Connection) -> None:
    """The outermost transaction ended without committing: drop everything."""
    take(connection)


def run(effects: list[Effect]) -> None:
    """Run committed effects; one that raises is logged, never fails the write."""
    for effect in effects:
        try:
            effect()
        except Exception:
            log.exception("after-commit effect failed")
