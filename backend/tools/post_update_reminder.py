"""The one-time ``post_update`` reminder on a human-started Turn's tool result.

``ToolRegistry`` appends the reminder to a result before any path persists it,
so it is stored with that result and every replay renders the same bytes; it is
never recomputed at render time. Every decision reads durable rows of the Turn,
so it survives a restart. The counts are content-free log lines with fixed
event names, the log-count mechanism the server-log triage already reads.
"""

from __future__ import annotations

import logging
from collections import deque

from infra.database import get_db
from infra.db_async import run_in_db
from tools.catalog_entries import POST_UPDATE_WORTH_SENDING

log = logging.getLogger(__name__)

#: Settled tool results a human-started Turn produces before the reminder.
SETTLED_RESULTS_BEFORE_REMINDER = 4

REMINDER_FIRST_LINE = (
    "[CORSIA reminder, once this Turn: the user has not been answered yet.]"
)
REMINDER = (
    f"{REMINDER_FIRST_LINE}\n"
    'To reply, call post_update(text="<your reply>"). '
    f"{POST_UPDATE_WORTH_SENDING} "
    "If no reply is needed yet, ignore this."
)

# Turns reminded by this process. The event loop runs the check-and-add below
# without an await between them, so parallel results of one step add it once;
# the stored rows cover a restart.
_reminded: deque[str] = deque(maxlen=4096)


#: Newest FIFO inputs searched for the Turn's human message. A Turn claims the
#: inputs that were waiting when it started, so they sit at the FIFO's newest end.
_INPUT_WINDOW = 100


def _wants_reminder(thread_id: str, turn_id: str) -> bool:
    """Read the Turn's own rows: four settled results, no send, human-started.

    Reads are bounded by the Turn's start, through the ``(thread_id, created_at)``
    index, and by the FIFO index, so a long thread's history is never scanned on
    a tool call.
    """
    from core.message_history import REPLY_VIA_POST_UPDATE_KEY

    db = get_db()
    try:
        turn = db.execute(
            "SELECT started_at FROM active_turns WHERE id=? AND thread_id=?",
            (turn_id, thread_id),
        ).fetchone()
        if turn is None:
            return False
        started_at = turn["started_at"]
        settled = db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND created_at>=? "
            "AND role='tool' AND json_valid(metadata) "
            "AND json_extract(metadata,'$.turn_id')=?",
            (thread_id, started_at, turn_id),
        ).fetchone()[0]
        if settled < SETTLED_RESULTS_BEFORE_REMINDER:
            return False
        sent = db.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND created_at>=? "
            "AND role='assistant' AND json_valid(metadata) "
            "AND json_extract(metadata,'$.for_user')=1 "
            "AND json_extract(metadata,'$.turn_id')=? LIMIT 1",
            (thread_id, started_at, turn_id),
        ).fetchone()
        if sent is not None:
            return False
        already = db.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND created_at>=? "
            "AND role='tool' AND json_valid(metadata) "
            "AND json_extract(metadata,'$.turn_id')=? "
            "AND instr(content, ?)>0 LIMIT 1",
            (thread_id, started_at, turn_id, REMINDER_FIRST_LINE),
        ).fetchone()
        if already is not None:
            return False
        human_started = db.execute(
            "SELECT 1 FROM (SELECT role,active_turn_id,metadata FROM messages "
            "WHERE thread_id=? AND input_order IS NOT NULL "
            "ORDER BY input_order DESC LIMIT ?) "
            "WHERE role='user' AND active_turn_id=? AND json_valid(metadata) "
            f"AND json_extract(metadata,'$.{REPLY_VIA_POST_UPDATE_KEY}')=1 LIMIT 1",
            (thread_id, _INPUT_WINDOW, turn_id),
        ).fetchone()
        return human_started is not None
    finally:
        db.close()


async def with_reply_reminder(
    result: object, *, tool_name: str, thread_id: str, turn_id: str | None,
) -> object:
    """Return ``result`` with the reminder appended when this result earns it.

    Only a plain successful text result is touched: an error result and a media
    result keep the start the harnesses read them by, and the reminder waits
    for the next plain one. Never raises: a failed read leaves the result as is.
    """
    if (
        type(result) is not str
        or result.startswith("Error")
        or tool_name == "post_update"
        or not turn_id
        or turn_id in _reminded
    ):
        return result
    try:
        wanted = await run_in_db(_wants_reminder, thread_id, turn_id)
    except Exception:
        log.warning("post_update reminder check failed thread=%s", thread_id, exc_info=True)
        return result
    if not wanted or turn_id in _reminded:
        return result
    _reminded.append(turn_id)
    log.info("post_update.reminder_emitted thread=%s turn=%s", thread_id, turn_id)
    return f"{result}\n\n{REMINDER}"


async def remind_current_turn(result: object, name: str, params: dict) -> object:
    """Apply ``with_reply_reminder`` for the executing thread and Turn.

    Reads the identity the registry's own execution runs under. A call that
    has no bound Turn is returned unchanged.
    """
    from infra import state
    from tools.tool_surface import resolve_dispatch

    thread_id = state._current_thread_id.get(None)
    identity = state._current_turn_identity.get(None)
    if not isinstance(thread_id, str) or identity is None:
        return result
    return await with_reply_reminder(
        result,
        tool_name=resolve_dispatch(name, params)[0],
        thread_id=thread_id,
        turn_id=getattr(identity, "turn_id", None),
    )
