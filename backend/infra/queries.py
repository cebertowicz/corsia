"""Named query helpers for common SQL patterns."""

from __future__ import annotations

import json
import uuid
from infra.chat_comment_answers import place_answers
from infra.delivery_states import INPUT_DELIVERY_ENDED, INPUT_DELIVERY_STATE_SET
from infra.time import utc_now_iso
from infra.turn_provenance import refuse_reader_only_reason, turn_provenance
from typing import Any

from infra.database import get_db, write_transaction


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def thread_available_sql(
    alias: str | None = None,
    *,
    include_user_archived: bool = False,
) -> str:
    """Return the canonical SQL predicate for a usable session row.

    Purged rows are never usable. Explicit user archives are excluded unless a
    listing explicitly asks to include them. Dormancy archives remain usable.
    """
    prefix = f"{alias}." if alias else ""
    state = f"COALESCE({prefix}lifecycle_state, 'active')"
    clauses = [f"{state} != 'purged'"]
    if not include_user_archived:
        clauses.append(
            f"NOT ({state} = 'archived' "
            f"AND COALESCE({prefix}archived_reason, '') = 'user')"
        )
    return "(" + " AND ".join(clauses) + ")"


def thread_unavailable_reason(session: Any | None) -> str | None:
    """Classify why a session cannot be opened, woken, or sent input."""
    if session is None:
        return "thread does not exist"
    state = _row_value(session, "lifecycle_state") or "active"
    if state == "purged":
        return "thread has been purged"
    if state == "archived" and _row_value(session, "archived_reason") == "user":
        return "thread is archived"
    return None


def thread_gone_for_good(session: Any | None) -> bool:
    """Whether a missing or purged thread cannot return from archive."""
    return session is None or _row_value(session, "lifecycle_state") == "purged"


def thread_is_available(session: Any | None) -> bool:
    return thread_unavailable_reason(session) is None


def thread_is_temporary(session: Any) -> bool:
    """Whether a spawned thread remains a temporary worker rather than an agent."""
    return _row_value(session, "spawned_by") is not None and not _row_value(session, "agent_enabled")


def thread_is_explicitly_archived(session: Any | None) -> bool:
    return (
        session is not None
        and (_row_value(session, "lifecycle_state") or "active") == "archived"
        and _row_value(session, "archived_reason") == "user"
    )


def get_thread(thread_id: str, db=None) -> Any | None:
    """Fetch a session by ID. Returns the row or None."""
    _db = db or get_db()
    row = _db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if not db:
        _db.close()
    return row


def load_thread_row(thread_id: str) -> dict:
    """Load a detached thread row and always close the owned connection."""
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        connection.close()


def thread_is_openable(thread_id: str, db=None) -> bool:
    """True when a session exists and is not archived — safe to open/navigate to.

    Resolves a conductor designation to an openable thread: the designation can
    dangle (the session was deleted) or point at an archived thread, and the
    Conductor nav must fall back to the landing in both cases.
    """
    row = get_thread(thread_id, db=db)
    return thread_is_available(row)


def get_thread_for_user(thread_id: str, user_id: str, db=None) -> Any | None:
    """Fetch a session by ID, scoped to a user. Returns the row or None.

    Used in HTTP routes and WebSocket handlers to validate ownership.
    """
    _db = db or get_db()
    row = _db.execute(
        "SELECT * FROM threads WHERE id = ? AND user_id = ?",
        (thread_id, user_id),
    ).fetchone()
    if not db:
        _db.close()
    return row


def user_in_project(project_id: str, user_id: str, db=None) -> bool:
    """True iff this user has any session in this project.

    The one project-membership rule. Authorization predicates that need it —
    plan scope mutation, and an external raise about a project-owned artifact —
    call this rather than restating the query, because two copies of a
    membership rule drift apart silently and neither copy looks wrong.
    """
    if not project_id or not user_id:
        return False
    conn = db or get_db()
    return conn.execute(
        "SELECT 1 FROM threads WHERE project_id=? AND user_id=? LIMIT 1",
        (project_id, user_id),
    ).fetchone() is not None


def get_thread_project_id(thread_id: str, db=None) -> str | None:
    """Return the project_id for a thread, or None."""
    _db = db if db is not None else get_db()
    try:
        row = _db.execute(
            "SELECT project_id FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return row["project_id"] if row else None
    finally:
        if db is None:
            _db.close()

def touch_thread(thread_id: str, db=None) -> None:
    """Update the activity timestamp under a named or standalone write owner."""
    with write_transaction(db, scope="touch_thread") as connection:
        connection.execute(
            "UPDATE threads SET updated_at = ? WHERE id = ?",
            (utc_now_iso(), thread_id),
        )


class TurnNotAcceptingToolCallsError(RuntimeError):
    """A tool call was declared on a Turn that has already reached terminal."""


def _declared_call_turn(metadata: dict | None) -> str | None:
    """Return the Turn a row would owe a tool result to, if it declares one."""
    if not metadata or not (
        metadata.get("llm_tool_calls") or metadata.get("tool_calls")
    ):
        return None
    turn_id = metadata.get("turn_id")
    return str(turn_id) if turn_id else None


def assert_turn_accepts_tool_calls(db, turn_id: str) -> None:
    """Refuse a tool call whose Turn can no longer answer it.

    A Turn settles its declared calls at its own terminal. That makes the owed
    state unreachable only while nothing declares a call *after* that terminal
    ran — a late provider frame otherwise writes a call onto a Turn that is
    already gone, recreating the declared-call-with-no-result state the
    terminal just closed, with no later terminal of its own to settle it.

    Read on the writer's own connection so the check and the row it guards
    share one transaction; a separate connection would leave the window it
    closes. A Turn with no row is not judged: nothing proves it ended, and a
    call bound to no Turn is already outside this ownership (it stays named by
    ``core.tool_result_debt.unsettled_tool_debt``).
    """
    row = db.execute(
        "SELECT status FROM active_turns WHERE id = ?", (turn_id,),
    ).fetchone()
    if row is not None and str(row[0]) != "running":
        raise TurnNotAcceptingToolCallsError(
            f"turn {turn_id} is {row[0]}, not running: a declared tool call "
            "would be owed a result no terminal can settle"
        )


def _validate_message_delivery(state: str, error: str | None) -> None:
    if state not in INPUT_DELIVERY_STATE_SET:
        raise ValueError(f"invalid message delivery state: {state!r}")
    if state == INPUT_DELIVERY_ENDED:
        if not isinstance(error, str) or not error.strip():
            raise ValueError("failed messages require a delivery_error")
    elif error is not None:
        raise ValueError(f"{state} messages must not carry a delivery_error")


def _insert_delivered_message(
    db,
    *,
    message_id: str,
    thread_id: str,
    role: str,
    content: str,
    metadata: dict | None,
    created_at: str | None,
) -> None:
    from thread.transcript_order import draw_transcript_position

    sequence = draw_transcript_position(db, thread_id)
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            message_id,
            thread_id,
            role,
            content,
            json.dumps(metadata) if metadata else None,
            created_at or utc_now_iso(),
            sequence,
        ),
    )
    place_answers(db, message_id, thread_id, role, content)


def _require_turn_provenance(message_id: str, role: str, metadata: dict | None) -> None:
    """Refuse an assistant or tool row the history projection would reject.

    One bad row fails every projection of its thread, so the writer applies the
    projection's own rule. User rows are exempt: a Turn binds them on claim.
    No row may carry a label only the projection assigns.
    """
    refuse_reader_only_reason(message_id, metadata or {})
    if role in ("assistant", "tool"):
        turn_provenance({"id": message_id, "_meta": metadata or {}})


def insert_message(
    thread_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
    msg_id: str | None = None,
    db=None,
    created_at: str | None = None,
    delivery_state: str = "delivered",
    delivery_error: str | None = None,
) -> str:
    """Insert one message using the delivery state's transcript contract.

    ``delivered`` rows receive a sequence immediately. Other states are
    intentionally transcript-ineligible here and retain a NULL sequence until
    an explicit transition orders them. A supplied
    connection remains caller-owned; without one this helper owns the complete
    transaction.
    """
    _validate_message_delivery(delivery_state, delivery_error)
    _id = msg_id or str(uuid.uuid4())
    _require_turn_provenance(_id, role, metadata)
    with write_transaction(db, scope="insert_message") as connection:
        # Inside the write transaction, so a Turn cannot reach terminal between
        # the check and the row it admits. This is the one row writer, so no
        # declaration path has to remember the rule.
        declaring_turn = _declared_call_turn(metadata)
        if declaring_turn is not None:
            assert_turn_accepts_tool_calls(connection, declaring_turn)

        if delivery_state == "delivered":
            _insert_delivered_message(
                connection,
                message_id=_id,
                thread_id=thread_id,
                role=role,
                content=content,
                metadata=metadata,
                created_at=created_at,
            )
        else:
            raise ValueError(
                "pending model-bound inputs must be admitted by the thread driver"
            )
        return _id


def get_project(project_id: str, db=None) -> Any | None:
    """Fetch a project by ID. Returns the row or None."""
    _db = db or get_db()
    row = _db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not db:
        _db.close()
    return row


def get_project_doc_roots(project_id: str, db=None) -> list[dict]:
    """Read a project's registered document roots."""
    _db = db or get_db()
    row = _db.execute(
        "SELECT doc_roots FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not db:
        _db.close()
    if not row or not row["doc_roots"]:
        return []
    return json.loads(row["doc_roots"])


def message_transcript_seq(thread_id: str, message_id: str, db=None) -> int | None:
    """Return one message's durable transcript position, or None if it has none.

    THE POSITION GOES ON THE WIRE. A live event frame that omits it leaves the
    client ordering that row by its timestamp, which is a guess -- and a wrong
    one whenever a row's clock and its position disagree. The symptom is a
    message appearing above the reply that answered it, curing itself when the
    user leaves the thread and comes back, because the REST fetch DOES carry
    sequences.

    A message that has not earned a position yet returns None, which is the
    honest answer: pending inputs deliberately hold no sequence.
    """
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT seq FROM messages WHERE id=? AND thread_id=?",
            (message_id, thread_id),
        ).fetchone()
    finally:
        if db is None:
            connection.close()
    if row is None:
        return None
    value = _row_value(row, "seq")
    return int(value) if isinstance(value, int) and value > 0 else None
