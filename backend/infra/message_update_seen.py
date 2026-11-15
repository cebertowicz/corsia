"""Monotonic evidence used by both replacement admission and unseen results."""
from contextlib import contextmanager
from dataclasses import dataclass

from infra import presence
from infra.presence_time import iso, parse


@dataclass(frozen=True)
class Seen:
    """A conservative seen verdict; an unknown stamp is not an actual viewing time."""
    at: str | None = None
    reason: str | None = None
    unknown_reason: str = "presence unavailable"


@contextmanager
def snapshot(user_id: str, thread_id: str):
    """Take a nonblocking presence snapshot, retaining its lock until the caller exits."""
    with presence.update_snapshot(user_id, thread_id) as live:
        yield live


def presence_verdict(db, user_id, thread_id, live):
    """Resolve process-local presence without treating a pending exit as a live claim."""
    row = db.execute("SELECT * FROM thread_presence WHERE user_id=? AND thread_id=?",
                     (user_id, thread_id)).fetchone()
    if live == "busy":
        return "unknown"
    if live and live[0] == "here":
        return "here"
    if row is None:
        return "never_opened"
    if live and live[1] is not None:
        return "away"
    return "unknown" if row["present"] else "away"


def record_assistant(db, message_id, thread_id, posted_at):
    """Capture original posting evidence even for a reply later promoted by the reminder."""
    owner = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    if owner is None:
        return
    with snapshot(owner[0], thread_id) as live:
        state = presence_verdict(db, owner[0], thread_id, live)
    reason = {"here": "presence_at_post", "unknown": "unknown"}.get(state)
    db.execute("INSERT OR IGNORE INTO message_update_state "
               "(message_id,thread_id,posted_at,presence_at_post,seen_at,seen_reason) VALUES (?,?,?,?,?,?)",
               (message_id, thread_id, iso(parse(posted_at)), state, posted_at if reason else None, reason))


def latch(db, thread_id, stamp, reason):
    """Keep viewing evidence before mutable visit/Mark-read fields are cleared."""
    db.execute("UPDATE message_update_state SET seen_at=?,seen_reason=? "
               "WHERE thread_id=? AND posted_at<=? AND seen_at IS NULL AND replaced_by IS NULL",
               (stamp, reason, thread_id, iso(parse(stamp))))


def seen(db, row, user_id, live, now):
    """Evaluate all four E2 clauses inside the replacement write transaction."""
    if row["seen_reason"]:
        return Seen(row["seen_at"], row["seen_reason"],
                    "posting evidence unavailable" if row["presence_at_post"] == "unknown" else "presence unavailable")
    if row["presence_at_post"] in ("here", "unknown"):
        return Seen(row["posted_at"], "unknown" if row["presence_at_post"] == "unknown" else "presence_at_post")
    visit = db.execute("SELECT last_entered_at,marked_read_at FROM thread_presence "
                       "WHERE user_id=? AND thread_id=?", (user_id, row["thread_id"])).fetchone()
    facts = []
    if visit:
        for column, reason in (("last_entered_at", "visit"), ("marked_read_at", "mark_read")):
            if visit[column] and parse(visit[column]) >= parse(row["posted_at"]):
                facts.append((visit[column], reason))
    prior = db.execute("SELECT MIN(entered_at) FROM presence_visits "
                       "WHERE user_id=? AND thread_id=? AND entered_at>=?",
                       (user_id, row["thread_id"], row["posted_at"])).fetchone()[0]
    comment = db.execute("SELECT MIN(c.created_at) FROM chat_comments c JOIN messages u ON u.id=c.user_message_id "
                         "AND u.thread_id=c.thread_id WHERE c.thread_id=? AND c.target_message_id=? "
                         "AND u.role='user' AND COALESCE(CASE WHEN json_valid(u.metadata) "
                         "THEN json_extract(u.metadata,'$.sender_kind') END,'')!='agent'",
                         (row["thread_id"], row["message_id"])).fetchone()[0]
    facts.extend((stamp, reason) for stamp, reason in ((prior, "visit"), (comment, "comment")) if stamp)
    if facts:
        stamp, reason = min(facts, key=lambda item: parse(item[0]))
        return Seen(stamp, reason)
    state = presence_verdict(db, user_id, row["thread_id"], live)
    if state in ("here", "unknown"):
        return Seen(now, "presence_now" if state == "here" else "unknown",
                    "presence check in progress" if live == "busy" else "presence unavailable")
    return Seen()
