"""Fold a feedback batch left on a retired identity into the live document's open batch, and undo it.

``migration_199_to_200`` re-keys each held feedback batch to the document's live identity. When the live
identity already holds an open batch for the same author, recipient and lane, a unique index forbids a second
one, and leaving the first behind would delay its agent hearing about the comments in it. So its items join the
live open batch, its edit snapshot (one per batch) moves with it when the live batch has none, and the emptied
batch row is removed; the live batch's count is recomputed from its items.

The live schema allows one event per (batch, comment) and one edit snapshot per batch. A batch is therefore not
folded when an item's comment is already in the live batch with a different reopen event, or when both batches
hold an edit snapshot. It stays where it is, counted by kind; ``feedback_delivery`` re-keys it to the live
document when its due time comes and delivers it there. The only foreign keys that point at a batch are its
items and its snapshot, both handled here.

A fold writes ledger rows to ``comment_identity_moves`` (the table the reverse reads): one
``doc_feedback_batch_folds`` row holding the removed batch exactly as it was and the live batch's items right
after the fold, one ``doc_feedback_batch_items`` row per item holding the batch it came from, and one
``doc_feedback_edit_items`` row when a snapshot moved. ``reverse_folds`` splits them back out, and only while the
live batch is still open and holds exactly the items the fold left it with.
"""

from __future__ import annotations

import json
import sqlite3

LEDGER = "comment_identity_moves"
FOLDS = "doc_feedback_batch_folds"
FOLDED_ITEMS = "doc_feedback_batch_items"
MOVED_EDIT = "doc_feedback_edit_items"
EDIT_ITEM_LEFT = "open_feedback_batch_edit_items_on_both"
EVENT_LEFT = "open_feedback_batch_event_conflict"
COLLISION_LEFT = "open_feedback_batch_collision"
_ITEM_COLUMNS = ("comment_id", "event", "event_at", "event_by")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _item_ids(conn: sqlite3.Connection, batch_id: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=? ORDER BY comment_id", (batch_id,))]


def _recount(conn: sqlite3.Connection, batch_id: str) -> None:
    conn.execute(
        "UPDATE doc_feedback_batches SET item_count=(SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?)"
        " WHERE id=?", (batch_id, batch_id),
    )


def _record(conn: sqlite3.Connection, table: str, row_id: str, original: dict, moved_to: str, now: str) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO {LEDGER}(table_name, row_id, original, moved_to, moved_at) VALUES (?,?,?,?,?)",
        (table, row_id, json.dumps(original), moved_to, now),
    )


def _conflicts(conn: sqlite3.Connection, items: list[dict], target_id: str) -> bool:
    """Whether an item's comment is already in the target batch with a different reopen event."""
    for item in items:
        held = conn.execute(
            "SELECT event, event_at, event_by FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
            (target_id, item["comment_id"]),
        ).fetchone()
        if held is not None and tuple(held) != (item["event"], item["event_at"], item["event_by"]):
            return True
    return False


def fold_batch(conn: sqlite3.Connection, batch_id: str, live: str, now: str) -> str | None:
    """Fold open batch ``batch_id`` into the live identity's open batch for the same lane.

    Returns None when folded, otherwise the reason it stayed (a key for the migration's left-behind count).
    """
    batch = _rows(conn, "SELECT * FROM doc_feedback_batches WHERE id=?", (batch_id,))[0]
    target = conn.execute(
        "SELECT id FROM doc_feedback_batches WHERE state='open' AND artifact_id=? AND author_kind=? AND author_id=?"
        " AND recipient_id=? AND delivery_policy=?",
        (live, batch["author_kind"], batch["author_id"], batch["recipient_id"], batch["delivery_policy"]),
    ).fetchone()
    if target is None:
        return COLLISION_LEFT
    target_id = target[0]
    items = _rows(conn, "SELECT * FROM doc_feedback_batch_items WHERE batch_id=?", (batch_id,))
    if _conflicts(conn, items, target_id):
        return EVENT_LEFT
    edits = _rows(conn, "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,))
    if edits and conn.execute("SELECT 1 FROM doc_feedback_edit_items WHERE batch_id=?", (target_id,)).fetchone():
        return EDIT_ITEM_LEFT
    for item in items:
        joined = conn.execute(
            "INSERT OR IGNORE INTO doc_feedback_batch_items(batch_id, comment_id, event, event_at, event_by)"
            " VALUES (?,?,?,?,?)", (target_id, *(item[column] for column in _ITEM_COLUMNS)),
        ).rowcount
        _record(conn, FOLDED_ITEMS, f"{batch_id}:{item['comment_id']}", {**item, "joined": bool(joined)}, target_id, now)
    for edit in edits:
        _record(conn, MOVED_EDIT, batch_id, edit, target_id, now)
        conn.execute("UPDATE doc_feedback_edit_items SET batch_id=? WHERE batch_id=?", (target_id, batch_id))
    conn.execute("DELETE FROM doc_feedback_batch_items WHERE batch_id=?", (batch_id,))
    conn.execute("DELETE FROM doc_feedback_batches WHERE id=?", (batch_id,))
    _recount(conn, target_id)
    _record(conn, FOLDS, batch_id, {"batch": batch, "target_items": _item_ids(conn, target_id)}, target_id, now)
    return None


def reverse_folds(conn: sqlite3.Connection, counts: dict[str, int]) -> None:
    """Split each folded batch back out, while the live batch is still as the fold left it; add to ``counts``."""
    for batch_id, original, target_id in conn.execute(
        f"SELECT row_id, original, moved_to FROM {LEDGER} WHERE table_name=?", (FOLDS,)
    ).fetchall():
        journal = json.loads(original)
        target = conn.execute("SELECT state FROM doc_feedback_batches WHERE id=?", (target_id,)).fetchone()
        if target is None or target[0] != "open" or _item_ids(conn, target_id) != sorted(journal["target_items"]):
            counts["changed_since"] += 1  # the live batch was delivered or changed: its items are no longer ours to move
            continue
        batch = journal["batch"]
        try:
            conn.execute(
                f"INSERT INTO doc_feedback_batches({', '.join(batch)}) VALUES ({', '.join('?' * len(batch))})",
                tuple(batch.values()),
            )
        except sqlite3.IntegrityError:
            counts["blocked"] += 1
            continue
        prefix = f"{batch_id}:"
        for entry_id, item_json in conn.execute(
            f"SELECT row_id, original FROM {LEDGER} WHERE table_name=? AND substr(row_id, 1, ?)=?",
            (FOLDED_ITEMS, len(prefix), prefix),
        ).fetchall():
            item = json.loads(item_json)
            try:
                conn.execute(
                    "INSERT INTO doc_feedback_batch_items(batch_id, comment_id, event, event_at, event_by) VALUES (?,?,?,?,?)",
                    (batch_id, *(item[column] for column in _ITEM_COLUMNS)),
                )
            except sqlite3.IntegrityError:
                pass  # its comment was deleted since
            if item["joined"]:
                conn.execute("DELETE FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
                             (target_id, item["comment_id"]))
            conn.execute(f"DELETE FROM {LEDGER} WHERE table_name=? AND row_id=?", (FOLDED_ITEMS, entry_id))
        if conn.execute(f"SELECT 1 FROM {LEDGER} WHERE table_name=? AND row_id=?", (MOVED_EDIT, batch_id)).fetchone():
            conn.execute("UPDATE doc_feedback_edit_items SET batch_id=? WHERE batch_id=?", (batch_id, target_id))
            conn.execute(f"DELETE FROM {LEDGER} WHERE table_name=? AND row_id=?", (MOVED_EDIT, batch_id))
        _recount(conn, batch_id)
        _recount(conn, target_id)
        conn.execute(f"DELETE FROM {LEDGER} WHERE table_name=? AND row_id=?", (FOLDS, batch_id))
        counts["reversed"] += 1
