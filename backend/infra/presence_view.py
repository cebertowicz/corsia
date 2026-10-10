"""Read-only views of user presence: the Plans read, the agent line, the tool.

``infra.presence`` owns the state; this module only derives from it. The
``User presence`` line and the ``user_presence`` tool render from one
``PresenceState``, so the pushed line and the pulled answer never disagree.
Wording and rules: design §3.1 and §3.5.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from infra import presence
from infra.time import utc_storage_iso_to_api

HERE, AWAY, UNKNOWN, NEVER_OPENED = "here", "away", "unknown", "never_opened"


@dataclass(frozen=True)
class PresenceState:
    """The user's presence in one conversation, as of one moment."""

    presence: str
    since: datetime | None
    last_reply_at: datetime | None
    here_since_last_reply: bool | None
    is_child: bool


def read_thread_presence(db: sqlite3.Connection, *, user_id: str, thread_id: str) -> dict:
    """Return the durable visit record for Plans: one primary-key lookup.

    While a visit is exit-pending ``present`` stays true until the exit is
    finalized (at most 2 min). "Since the user's last entry" at open time
    needs ``previous_entered_at`` while ``present`` is true, because
    ``last_entered_at`` is then the current visit.
    """
    row = db.execute(
        "SELECT present, last_entered_at, last_exited_at, previous_entered_at "
        "FROM thread_presence WHERE user_id=? AND thread_id=?",
        (user_id, thread_id),
    ).fetchone()
    if row is None:
        return {"recorded": False, "reason": NEVER_OPENED}
    return {
        "recorded": True,
        "present": bool(row["present"]),
        "last_entered_at": utc_storage_iso_to_api(row["last_entered_at"]),
        "last_exited_at": utc_storage_iso_to_api(row["last_exited_at"]),
        "previous_entered_at": utc_storage_iso_to_api(row["previous_entered_at"]),
    }


def _last_reply_at(db: sqlite3.Connection, thread_id: str) -> datetime | None:
    """The latest assistant row with visible text: a reverse read on (thread, seq)."""
    row = db.execute(
        "SELECT created_at FROM messages WHERE thread_id=? AND seq IS NOT NULL "
        "AND role='assistant' AND length(trim(content)) > 0 "
        "ORDER BY seq DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return presence.parse(row["created_at"]) if row else None


def _state_and_since(row, user_id: str, thread_id: str) -> tuple[str, datetime | None]:
    if row is None:
        return NEVER_OPENED, None
    entered = presence.parse(row["last_entered_at"])
    live = presence.live_view(user_id, thread_id)
    if live is not None and live[0] == HERE:
        return HERE, entered
    if live is not None and live[1] is not None:
        return AWAY, max(live[1], entered)
    if row["present"]:
        last_here = row["exit_pending_at"] or row["last_confirmed_at"]
        return UNKNOWN, presence.parse(last_here)
    return AWAY, presence.parse(row["last_exited_at"])


def _seen(state: str, since: datetime | None, reply: datetime | None) -> bool | None:
    if reply is None or state == UNKNOWN:
        return None
    if state == HERE:
        return True
    if state == NEVER_OPENED or since is None:
        return False
    return since >= reply


def presence_state(db: sqlite3.Connection, thread_id: str) -> PresenceState | None:
    """Derive the owning user's presence in ``thread_id``; None if no such thread."""
    thread = db.execute(
        "SELECT user_id, parent_thread_id, spawned_by FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if thread is None:
        return None
    row = db.execute(
        "SELECT * FROM thread_presence WHERE user_id=? AND thread_id=?",
        (thread["user_id"], thread_id),
    ).fetchone()
    state, since = _state_and_since(row, thread["user_id"], thread_id)
    reply = _last_reply_at(db, thread_id)
    return PresenceState(
        presence=state,
        since=since,
        last_reply_at=reply,
        here_since_last_reply=_seen(state, since, reply),
        is_child=bool(thread["parent_thread_id"] or thread["spawned_by"]),
    )


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone()


def _clock(value: datetime, as_of: datetime) -> str:
    local, reference = _local(value), _local(as_of)
    if local.date() == reference.date():
        return f"{local:%H:%M %Z}"
    return f"{local:%Y-%m-%d %H:%M %Z}"


def _duration(span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    hours = minutes // 60
    if hours < 1:
        return f"{minutes} min"
    if hours < 12:
        return f"{hours} h {minutes % 60} min"
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d"


def render_line(state: PresenceState, *, as_of: datetime) -> str | None:
    """Render the ``User presence`` line; None for a never-opened child thread."""
    if state.presence == NEVER_OPENED and state.is_child:
        return None
    head = f"User presence ({_local(as_of):%Y-%m-%d %H:%M %Z}): "
    if state.presence == NEVER_OPENED or state.since is None:
        return head + "the user has never opened this conversation."
    since = _clock(state.since, as_of)
    if state.presence == HERE:
        return head + f"here now, since {since}."
    if state.presence == UNKNOWN:
        return head + (
            "unknown until the user's browser reconnects after a server "
            f"restart; last here {since}."
        )
    body = f"away since {since} ({_duration(_naive(as_of) - state.since)})"
    if state.last_reply_at is None:
        return head + body + "."
    if state.here_since_last_reply:
        return head + body + "; was here after your last reply."
    return head + body + (
        "; has not been here since your last reply at "
        f"{_clock(state.last_reply_at, as_of)}."
    )


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def presence_line(db: sqlite3.Connection, thread_id: str, *, as_of: datetime) -> str | None:
    """The ``User presence`` line for ``thread_id`` as of ``as_of``.

    Returns None for a child thread its user never opened (its reader is its
    parent) and for a thread that does not exist. ``as_of`` is naive UTC or
    timezone-aware; times render in the server's local zone.
    """
    state = presence_state(db, thread_id)
    return None if state is None else render_line(state, as_of=as_of)


def _api(value: datetime | None) -> str | None:
    return None if value is None else f"{_naive(value):%Y-%m-%dT%H:%M:%SZ}"


def user_presence_payload(db: sqlite3.Connection, thread_id: str, *, as_of: datetime) -> dict | None:
    """The ``user_presence`` tool result; ``line`` equals ``presence_line``."""
    state = presence_state(db, thread_id)
    if state is None:
        return None
    return {
        "presence": state.presence,
        "as_of": _api(as_of),
        "since": _api(state.since),
        "last_reply_at": _api(state.last_reply_at),
        "here_since_last_reply": state.here_since_last_reply,
        "line": render_line(state, as_of=as_of),
    }
