"""The thread update index: what lights a thread's dot and becomes its cards.

Two writers only. ``post_update`` records its own message as a ``marked``
update inside its write transaction; the Structured Plans projector records a
qualifying plan outcome through ``record_plan_update`` inside the plan
mutation's transaction. Nothing infers an update from an ordinary assistant
message or a Turn ending.

A row stores the plan item's title as it read when the outcome happened and
never a raw reason, evidence, feedback or note, so a card reads the same after
the node is renamed or deleted. The source event is the idempotency key:
``INSERT ... ON CONFLICT(source_kind, source_id) DO NOTHING`` makes a replay a
no-op while a CHECK violation still raises and rolls the caller back.

Two watermarks, deliberately separate. The update dot, the "Since your last
visit" divider and Mark read/unread read only the committed presence row
(its visit exit, raised by ``marked_read_at``, held on by
``marked_unread_at``). The Failed mark reads the plan pane's
``plan_pane_views.seen_through``, which only opening the plan advances. So
Mark read/unread moves the dot and nothing else; the chat list offers it only
while the dot is the mark a row shows, never under a Failed or owed word.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Literal, get_args

from infra import presence
from infra.thread_update_asks import ask_sql, asks_user_sql
from infra.presence_time import iso, parse, seen_boundary
from infra.visit_divider import divider_from_row

log = logging.getLogger(__name__)

PlanUpdateKind = Literal["done", "done_follow_up", "failed", "closed", "blocked"]
PLAN_UPDATE_KINDS: frozenset[str] = frozenset(get_args(PlanUpdateKind))

#: The most threads one row read names; matches the chat list's state cap.
THREAD_LIMIT = 200
#: The most cards a transcript read retains; one extra row proves older omission.
CARD_WINDOW_LIMIT = 1_000
#: The most cards an internal forward page returns.
PAGE_LIMIT = 100

CHANGED_EVENT = "thread_updates_changed"

_COLUMNS = "id,thread_id,kind,source_kind,source_id,node_id,title,after_seq,created_at"

def canonical_time(value: str) -> str:
    """One ordering shape for every source: naive UTC with microseconds."""
    return iso(parse(value))


def _after_seq(db: sqlite3.Connection, thread_id: str) -> int:
    """The transcript position an update follows; 0 precedes the first message."""
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE thread_id=?", (thread_id,),
    ).fetchone()
    return int(row[0])


def _record(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    kind: str,
    source_kind: str,
    source_id: str,
    node_id: str | None,
    title: str | None,
    created_at: str,
) -> bool:
    """Insert one update unless its source already has one; True when inserted."""
    cursor = db.execute(
        f"""INSERT INTO thread_updates ({_COLUMNS.removeprefix('id,')})
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(source_kind, source_id) DO NOTHING""",
        (
            thread_id, kind, source_kind, source_id, node_id, title,
            _after_seq(db, thread_id), canonical_time(created_at),
        ),
    )
    return cursor.rowcount == 1


def record_marked(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    message_id: str,
    node_id: str | None,
    created_at: str,
) -> bool:
    """Record an explicit ``post_update`` message; call inside its transaction."""
    return _record(
        db, thread_id=thread_id, kind="marked", source_kind="message",
        source_id=message_id, node_id=node_id, title=None, created_at=created_at,
    )


def record_plan_update(
    db: sqlite3.Connection,
    *,
    thread_id: str,
    kind: PlanUpdateKind,
    event_id: str,
    node_id: str,
    created_at: str,
) -> bool:
    """Record one qualifying plan outcome for the Structured Plans projector.

    The caller decides qualification (owner, top-level or user-assigned node,
    outcome, block suppression) and calls this inside the plan mutation's
    transaction. The node's current title is snapshotted here; a missing node
    or blank title raises rather than committing an unreadable card, and so
    does any kind that is not a card outcome (``hold_cleared``, a reply).
    """
    if kind not in PLAN_UPDATE_KINDS:
        raise ValueError(f"{kind!r} is not a plan update outcome")
    row = db.execute("SELECT title FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    title = str(row[0]).strip() if row is not None and row[0] is not None else ""
    if not title:
        raise ValueError(f"plan node {node_id!r} has no readable title to snapshot")
    return _record(
        db, thread_id=thread_id, kind=kind, source_kind="plan_event",
        source_id=event_id, node_id=node_id, title=title, created_at=created_at,
    )


def _public(row: sqlite3.Row | tuple) -> dict:
    """The fields a reader may see: no source text, reason or evidence exists."""
    (update_id, thread_id, kind, source_kind, source_id, node_id, title,
     after_seq, created_at) = tuple(row)
    return {
        "id": update_id,
        "thread_id": thread_id,
        "kind": kind,
        "message_id": source_id if source_kind == "message" else None,
        "node_id": node_id,
        "title": title,
        "after_seq": after_seq,
        "created_at": created_at,
    }


_INLINE_MARKDOWN = (
    re.compile(r"!?\[([^\]]+)\]\([^)]+\)"),
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"__(?=\S)(.+?)(?<=\S)__"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
    re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)"),
    re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])"),
    re.compile(r"`(?=\S)(.+?)(?<=\S)`"),
)


def _strip_inline_markdown(line: str) -> str:
    """Remove bounded inline Markdown while preserving ordinary punctuation."""
    previous = None
    while line != previous:
        previous = line
        for pattern in _INLINE_MARKDOWN:
            line = pattern.sub(r"\1", line)
    return line


def _first_readable_line(content: object) -> str | None:
    """The first non-code line of a marked message, without common Markdown chrome."""
    if not isinstance(content, str):
        return None
    in_fence = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        line = re.sub(r"^(?:(?:#{1,6}|[-+*]|\d+[.)])\s+|>\s*)+", "", line)
        line = re.sub(r"^\[[ xX]\]\s+", "", line)
        line = _strip_inline_markdown(line).strip()
        if line:
            return line
    return None



def _summary(update: dict, message_content: object) -> str | None:
    """A stored event's sidebar preview; lifecycle text comes only from its snapshot."""
    if update["kind"] == "marked":
        return _first_readable_line(message_content)
    title = update["title"]
    if not title:
        return None
    labels = {
        "done": "Completed",
        "done_follow_up": "Completed",
        "closed": "Closed",
        "failed": "Failed",
        "blocked": "Blocked",
    }
    label = labels.get(update["kind"])
    if label is None:
        return None
    suffix = " · Follow-up added" if update["kind"] == "done_follow_up" else ""
    return f"{label}: {title}{suffix}"


def latest_by_thread_with_attention(
    db: sqlite3.Connection, thread_ids: list[str],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return newest display and newest attention events from one bounded indexed read.

    A completion can be the useful newest sidebar preview without replacing an
    earlier unread marked/failure/block event that controls the update dot.
    """
    ids = list(dict.fromkeys(thread_ids))
    if len(ids) > THREAD_LIMIT:
        raise ValueError(f"read at most {THREAD_LIMIT} threads at once; {len(ids)} were named")
    if not ids:
        return {}, {}
    newest = "created_at || '/' || printf('%019d', id)"
    row_key = "u.created_at || '/' || printf('%019d', u.id)"
    columns = ",".join(f"u.{column}" for column in _COLUMNS.split(","))
    rows = db.execute(
        f"""WITH selected AS (
                SELECT thread_id,
                       MAX({newest}) AS display_key,
                       MAX(CASE WHEN kind NOT IN ('done', 'done_follow_up', 'closed')
                                 AND (kind != 'failed' OR {asks_user_sql()})
                                THEN {newest} END) AS attention_key
                  FROM thread_updates INDEXED BY idx_thread_updates_thread_time
                 WHERE thread_id IN ({','.join('?' * len(ids))})
                 GROUP BY thread_id
            )
            SELECT {columns}, messages.content AS message_content,
                   selected.display_key, selected.attention_key,
                   {row_key} AS row_key
              FROM selected
              CROSS JOIN thread_updates u INDEXED BY idx_thread_updates_thread_time
                ON u.thread_id=selected.thread_id
                AND ({row_key}=selected.display_key OR {row_key}=selected.attention_key)
              LEFT JOIN messages ON messages.id=u.source_id AND u.source_kind='message'""",
        ids,
    ).fetchall()
    display: dict[str, dict] = {}
    attention: dict[str, dict] = {}
    width = len(_COLUMNS.split(","))
    for row in rows:
        update = _public(tuple(row)[:width])
        update["summary"] = _summary(update, row["message_content"])
        if row["row_key"] == row["display_key"]:
            display[update["thread_id"]] = update
        if row["row_key"] == row["attention_key"]:
            attention[update["thread_id"]] = update
    return (
        {thread_id: display[thread_id] for thread_id in ids if thread_id in display},
        {thread_id: attention[thread_id] for thread_id in ids if thread_id in attention},
    )


def latest_by_thread(db: sqlite3.Connection, thread_ids: list[str]) -> dict[str, dict]:
    """Each named thread's newest stored event, including passive sidebar preview."""
    display, _attention = latest_by_thread_with_attention(db, thread_ids)
    return display


def _unseen(row: sqlite3.Row | None, created_at: str) -> bool:
    """Whether an update at ``created_at`` is past the committed seen boundary."""
    since = seen_boundary(row)
    return row is None or (since is not None and created_at > since)


def seen_by_thread(
    db: sqlite3.Connection, user_id: str, latest: dict[str, dict], thread_ids: list[str],
) -> dict[str, dict]:
    """Each named thread's update dot, from committed presence columns only.

    The dot is on when Mark unread holds, or when the newest update is newer
    than the seen boundary: the visit's exit (or now, while present), raised by
    Mark read. A thread never opened has seen nothing. ``opened`` says whether
    Mark read/unread has a record to write. The in-memory claim is never read,
    so a failed entry write leaves the dot on. Threads absent from the result
    have neither a record nor an update.
    """
    ids = list(dict.fromkeys(thread_ids))
    if len(ids) > THREAD_LIMIT:
        raise ValueError(f"read at most {THREAD_LIMIT} threads at once; {len(ids)} were named")
    if not ids:
        return {}
    rows = {
        str(row["thread_id"]): row
        for row in db.execute(
            f"""SELECT thread_id,present,exit_pending_at,last_exited_at,marked_read_at,
                       marked_unread_at FROM thread_presence
                 WHERE user_id=? AND thread_id IN ({','.join('?' * len(ids))})""",
            (user_id, *ids),
        )
    }
    seen: dict[str, dict] = {}
    for thread_id in ids:
        row, update = rows.get(thread_id), latest.get(thread_id)
        if row is None and update is None:
            continue
        newer = update is not None and _unseen(row, update["created_at"])
        seen[thread_id] = {
            "has_update": bool(row is not None and row["marked_unread_at"]) or newer,
            "opened": row is not None,
        }
    return seen



def read_newest_cards(
    db: sqlite3.Connection, thread_id: str, *, min_seq: int,
) -> tuple[list[dict], bool]:
    """The newest bounded card window after ``min_seq``, ordered for the transcript.

    The fixed 1,001-row reverse read is covered by the thread/sequence index:
    the extra row proves that an older card was omitted, while the retained
    cards return in normal ``(after_seq, id)`` order for transcript merging.
    """
    rows = db.execute(
        f"""SELECT {_COLUMNS}, EXISTS (
                SELECT 1 FROM plan_nodes n
                 WHERE n.id = thread_updates.node_id AND n.archived_at IS NULL
             ) AS node_exists, {asks_user_sql()} AS asks_user,
             {asks_user_sql(blocked=True)} AS blocked_asks_user,
             {ask_sql('id')} AS ask_hold_id, {ask_sql('owner_id')} AS ask_artifact_id
             FROM thread_updates
             WHERE thread_id=? AND after_seq >= ? AND kind != 'marked'
             ORDER BY after_seq DESC, id DESC LIMIT ?""",
        (thread_id, min_seq, CARD_WINDOW_LIMIT + 1),
    ).fetchall()
    has_more = len(rows) > CARD_WINDOW_LIMIT
    retained = rows[:CARD_WINDOW_LIMIT]
    cards = [
        {
            **_public(tuple(row)[:-5]),
            "node_exists": bool(row["node_exists"]),
            "asks_user": bool(row["asks_user"]),
            "blocked_asks_user": bool(row["blocked_asks_user"]),
            # The user ask this update recorded; the API hydrates it into a card.
            "ask": (
                {"hold_id": row["ask_hold_id"], "artifact_id": row["ask_artifact_id"]}
                if row["ask_hold_id"] else None
            ),
        }
        for row in reversed(retained)
    ]
    return cards, has_more



def visit_divider(db: sqlite3.Connection, user_id: str, thread_id: str) -> dict | None:
    """Where "Since your last visit" goes, frozen when a visit has opened."""
    frozen, divider = presence.frozen_visit_divider(user_id, thread_id)
    if frozen:
        return divider
    row = db.execute(
        """SELECT present,exit_pending_at,last_exited_at,marked_read_at FROM thread_presence
            WHERE user_id=? AND thread_id=?""",
        (user_id, thread_id),
    ).fetchone()
    return divider_from_row(db, row, thread_id)


def delete_thread_updates(db: sqlite3.Connection, thread_id: str) -> None:
    """Delete a thread's updates and their title snapshots (thread delete).

    SQLite foreign keys are off at runtime, so thread deletion calls this
    explicitly, inside its transaction, before the thread row goes.
    """
    db.execute("DELETE FROM thread_updates WHERE thread_id=?", (thread_id,))


def notify_changed(user_id: str, thread_ids: list[str]) -> None:
    """Best-effort after-commit nudge; the next bounded read recovers a miss."""
    from infra import state

    try:
        state.broadcast_to_user(user_id, {"type": CHANGED_EVENT, "thread_ids": list(thread_ids)})
    except Exception as exc:  # noqa: BLE001 - committed rows remain authoritative
        log.warning("thread_updates_changed delivery failed: %s", type(exc).__name__)
