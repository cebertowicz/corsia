"""The app-wide "here" signal (R4): is the person using CORSIA right now, on any view.

Its own WebSocket message, `{"type": "here", "state": "here" | "away", "hidden": bool}`. It is not a thread claim: it
never touches `thread_presence`, the visit record, seen marks or the thread-presence claims in `infra.presence`.

Kept per live connection, in memory only; nothing is written to the database. The page decides `here` (visible, input in
the last 10 minutes) and sends `away` when it stops being true. A page that goes hidden sends `away` with `hidden: true`
and the server counts it as here for the existing 30-second hidden grace (`presence.HIDDEN_GRACE`), as thread presence does.
A closed connection is dropped when its socket closes; a connection that dies without closing is closed by today's
keepalive (a 30 s ping, closed on a failed send), so it stops counting within about a minute. Claims live in one process,
the same assumption the thread-presence claims make.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from infra.presence import HIDDEN_GRACE


@dataclass
class _Connection:
    user_id: str
    here: bool = False
    #: Monotonic time the page said it was hidden, or None while it is visible.
    hidden_since: float | None = None


_connections: dict[str, _Connection] = {}
#: The clock, replaceable by a test.
clock: Callable[[], float] = time.monotonic


def apply(session: str, user_id: str, data: dict) -> None:
    """Apply one `here` message from one connection. A malformed message changes nothing."""
    state = data.get("state")
    if state not in ("here", "away"):
        return
    connection = _connections.setdefault(session, _Connection(user_id))
    if state == "here":
        connection.here, connection.hidden_since = True, None
    else:
        connection.here = False
        connection.hidden_since = None
        if data.get("hidden") is True:
            # Hidden is not yet away: it stays here through the grace, then reads away.
            connection.here, connection.hidden_since = True, clock()


def drop(session: str) -> None:
    """The connection closed."""
    _connections.pop(session, None)


def is_here(user_id: str | None = None) -> bool:
    """True when any live connection of the person reports here (any person's, when no id is given: an install has one)."""
    now = clock()
    grace = HIDDEN_GRACE.total_seconds()
    return any(
        connection.here and (connection.hidden_since is None or now - connection.hidden_since < grace)
        for connection in _connections.values()
        if user_id is None or connection.user_id == user_id
    )
