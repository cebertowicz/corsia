"""Schema 199 -> 200: move the comments left on retired identities to the document's live identity.

A promote moves a document's comments to the identity it hands the document to (``retire``), so new
comments never stay on a retired one. Older rows can: promotes made before that move existed, and
comments an old pane saved on a retired id. They are invisible once a read follows the document to its
live identity, so this step moves them there, walking the whole successor chain to the identity that is
active now.

What moves is what ``doceditor.comment_transfer`` moves at a promote: the comments (their tags,
mentions, recipient state, acknowledgements and read marks key by comment id and follow), the held
feedback batches, the review requests and the writers' lines. A section writer line is re-keyed to the live
document and its saved calls pointed at its path. Nothing is merged: comment ids and a thread's position are not
changed. Image region numbers are unique per document and image (``idx_doc_comments_region_number``), so a
moved region comment whose number the live document already uses on that image takes the next free number there;
its old number is in the ledger and the reverse restores it. Numbers that do not collide are kept.

An open feedback batch that would collide with the live identity's open batch for the same author, recipient and
lane (a unique index) is folded into it: its items join that batch, so its agent still hears about them
(``infra.comment_identity_batch_folds``; the reverse splits them back out).

What stays behind, counted and logged: a reply whose parent is a top-level comment on a document outside the
lineage; a chain that ends at no active identity; an open batch with an edit item (it delivers to the live
document when due, ``feedback_delivery``); a section line that would collide with one the live identity already
holds. Comment rows always move, even when the batch or line beside them stays.

Every moved row's original values go to ``comment_identity_moves`` in the same statement batch, and
``reverse_moves`` (run by ``scripts/migrations/reverse_comment_identity_move.py``) puts them back. Running the
step twice changes nothing. Runs inside the runner's transaction; it never commits.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from infra.comment_identity_batch_folds import LEDGER, fold_batch, reverse_folds
from infra.doc_comments_schema import COMMENT_IDENTITY_MOVES_SQL

CHUNK = 400  # ids per statement, below SQLite's bound-variable limit
_SECTION = "section:"  # doceditor.writer_turns: a section line's root_id names the document identity
# The columns a move changes, per table: the only ones the ledger records and the reverse restores.
MOVED_COLUMNS = {
    "doc_comments": ("artifact_id", "region_number"),
    "doc_feedback_batches": ("artifact_id",),
    "doc_review_requests": ("artifact_id",),
    "doc_comment_writer_line": ("artifact_id", "root_id", "ops"),
}
_PARENT_OUTSIDE = "parent_outside_lineage"
_DEAD_END = "no_live_identity"


@dataclass
class MoveReport:
    """What one run moved and left, by table and by cause."""

    moved: dict[str, int] = field(default_factory=lambda: {table: 0 for table in MOVED_COLUMNS})
    left_behind: dict[str, int] = field(default_factory=dict)
    renumbered: int = 0  # moved region comments that took a new number to stay unique on the live document
    folded: int = 0  # open batches whose items joined the live document's open batch

    def left(self, key: str, count: int = 1) -> None:
        self.left_behind[key] = self.left_behind.get(key, 0) + count

    def line(self) -> str:
        moved = " ".join(f"{table}={count}" for table, count in self.moved.items())
        left = " ".join(f"{key}={count}" for key, count in sorted(self.left_behind.items())) or "none"
        return f"moved: {moved}; region numbers changed: {self.renumbered}; batches folded: {self.folded}; left behind: {left}"


def _final_identity(conn: sqlite3.Connection, start: str, cache: dict[str, str | None]) -> str | None:
    """The active identity ``start``'s successor chain ends at; ``start`` itself when active; None at a dead end or loop."""
    if start in cache:
        return cache[start]
    seen, current, result = {start}, start, None
    while True:
        row = conn.execute(
            "SELECT state, CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.successor_id') END"
            " FROM artifact_identities WHERE id=?",
            (current,),
        ).fetchone()
        if row is None:
            break
        state, successor = row
        if state == "active":
            result = current
            break
        if not successor or successor in seen:
            break
        seen.add(successor)
        current = successor
    cache[start] = result
    return result


def _path(conn: sqlite3.Connection, identity: str) -> str | None:
    row = conn.execute("SELECT path FROM artifact_identities WHERE id=?", (identity,)).fetchone()
    return row[0] if row else None


def _repoint(ops_json: str, old_path: str | None, new_path: str | None) -> str:
    """The saved calls with each ``path`` that names the old file taken to the new one, otherwise verbatim."""
    if not old_path or not new_path:
        return ops_json
    try:
        ops = json.loads(ops_json)
    except (TypeError, ValueError):
        return ops_json
    changed = False
    for op in ops if isinstance(ops, list) else []:
        call = op.get("call") if isinstance(op, dict) else None
        if not isinstance(call, dict):
            continue
        path = call.get("path")
        if isinstance(path, str) and (path == old_path or old_path.endswith("/" + path)):
            call["path"] = new_path
            changed = True
    return json.dumps(ops) if changed else ops_json


def _chunks(ids: Sequence[str]):
    for start in range(0, len(ids), CHUNK):
        yield ids[start:start + CHUNK]


def _record(conn: sqlite3.Connection, table: str, row_id: str, original: dict, moved_to: str, now: str) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO {LEDGER}(table_name, row_id, original, moved_to, moved_at) VALUES (?,?,?,?,?)",
        (table, row_id, json.dumps(original), moved_to, now),
    )


def _move_by_ids(conn: sqlite3.Connection, table: str, ids: Sequence[str], live: str, now: str) -> int:
    """Record and re-key rows that change only ``artifact_id``, one statement pair per chunk."""
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        conn.execute(
            f"INSERT OR IGNORE INTO {LEDGER}(table_name, row_id, original, moved_to, moved_at)"
            f" SELECT ?, id, json_object('artifact_id', artifact_id), ?, ? FROM {table} WHERE id IN ({marks})",
            (table, live, now, *chunk),
        )
        conn.execute(f"UPDATE {table} SET artifact_id=? WHERE id IN ({marks})", (live, *chunk))
    return len(ids)


def _move_comments(conn, source, live, now, cache, report) -> None:
    rows = conn.execute(
        "SELECT c.id, c.parent_id, p.artifact_id FROM doc_comments c"
        " LEFT JOIN doc_comments p ON p.id = c.parent_id WHERE c.artifact_id=?",
        (source,),
    ).fetchall()
    movable: list[str] = []
    for comment_id, parent_id, parent_artifact in rows:
        if parent_id is not None and not (
            parent_artifact is not None
            and (parent_artifact in (source, live) or _final_identity(conn, parent_artifact, cache) == live)
        ):
            report.left(_PARENT_OUTSIDE)
            continue
        movable.append(comment_id)
    clashing = _region_clashes(conn, movable, live)
    plain = [comment_id for comment_id in movable if comment_id not in clashing]
    report.moved["doc_comments"] += _move_by_ids(conn, "doc_comments", plain, live, now)
    for comment_id in clashing:  # after the rest, so each takes the next number above what the live document holds
        number = conn.execute("SELECT region_number FROM doc_comments WHERE id=?", (comment_id,)).fetchone()[0]
        _record(conn, "doc_comments", comment_id, {"artifact_id": source, "region_number": number}, live, now)
        conn.execute(
            "UPDATE doc_comments SET artifact_id=:live, region_number=(SELECT COALESCE(MAX(o.region_number), 0) + 1"
            " FROM doc_comments o WHERE o.artifact_id=:live AND COALESCE(o.image_src, '')=COALESCE(doc_comments.image_src, ''))"
            " WHERE id=:id",
            {"live": live, "id": comment_id},
        )
        report.moved["doc_comments"] += 1
        report.renumbered += 1


def _region_clashes(conn: sqlite3.Connection, ids: Sequence[str], live: str) -> list[str]:
    """The ids, in number order, whose region number the live document already uses on the same image."""
    clashing: list[str] = []
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        clashing += [row[0] for row in conn.execute(
            f"SELECT c.id FROM doc_comments c WHERE c.id IN ({marks}) AND c.region_number IS NOT NULL AND EXISTS ("
            " SELECT 1 FROM doc_comments o WHERE o.artifact_id=? AND COALESCE(o.image_src, '')=COALESCE(c.image_src, '')"
            " AND o.region_number=c.region_number) ORDER BY c.region_number, c.id",
            (*chunk, live),
        )]
    return clashing


def _move_batches(conn, source, live, now, report) -> None:
    for (batch_id,) in conn.execute("SELECT id FROM doc_feedback_batches WHERE artifact_id=?", (source,)).fetchall():
        moved = conn.execute(
            "UPDATE doc_feedback_batches SET artifact_id=:live WHERE id=:id AND NOT (state='open' AND EXISTS ("
            " SELECT 1 FROM doc_feedback_batches o WHERE o.state='open' AND o.id != doc_feedback_batches.id"
            " AND o.artifact_id=:live AND o.author_kind=doc_feedback_batches.author_kind"
            " AND o.author_id=doc_feedback_batches.author_id AND o.recipient_id=doc_feedback_batches.recipient_id"
            " AND o.delivery_policy=doc_feedback_batches.delivery_policy))",
            {"live": live, "id": batch_id},
        ).rowcount
        if moved:
            _record(conn, "doc_feedback_batches", batch_id, {"artifact_id": source}, live, now)
            report.moved["doc_feedback_batches"] += 1
        else:
            left = fold_batch(conn, batch_id, live, now)
            if left is None:
                report.folded += 1
            else:
                report.left(left)


def _move_writer_lines(conn, source, live, now, report) -> None:
    old_path, new_path = _path(conn, source), _path(conn, live)
    prefix = f"{_SECTION}{source}:"
    for line_id, root_id, ops in conn.execute(
        "SELECT id, root_id, ops FROM doc_comment_writer_line WHERE artifact_id=?", (source,)
    ).fetchall():
        new_root = f"{_SECTION}{live}:{root_id[len(prefix):]}" if root_id.startswith(prefix) else root_id
        moved = conn.execute(
            "UPDATE doc_comment_writer_line SET artifact_id=:live, root_id=:root, ops=:ops WHERE id=:id"
            " AND NOT EXISTS (SELECT 1 FROM doc_comment_writer_line o WHERE o.root_id=:root"
            " AND o.agent_thread_id=doc_comment_writer_line.agent_thread_id AND o.id != doc_comment_writer_line.id)",
            {"live": live, "root": new_root, "ops": _repoint(str(ops), old_path, new_path), "id": line_id},
        ).rowcount
        if moved:
            _record(conn, "doc_comment_writer_line", line_id,
                    {"artifact_id": source, "root_id": root_id, "ops": ops}, live, now)
            report.moved["doc_comment_writer_line"] += 1
        else:
            report.left("section_writer_line_collision")


def move_comments_to_live_identities(conn: sqlite3.Connection) -> MoveReport:
    """Move every retired identity's comment state to its live identity; see the module docstring."""
    report = MoveReport()
    now = datetime.now(timezone.utc).isoformat()
    cache: dict[str, str | None] = {}
    holders = (
        "SELECT artifact_id FROM doc_comments UNION SELECT artifact_id FROM doc_feedback_batches"
        " UNION SELECT artifact_id FROM doc_review_requests UNION SELECT artifact_id FROM doc_comment_writer_line"
    )
    retired = conn.execute(
        "SELECT id FROM artifact_identities WHERE state != 'active' AND json_valid(metadata)"
        " AND json_extract(metadata, '$.successor_id') IS NOT NULL"
        f" AND id IN ({holders}) ORDER BY id"
    ).fetchall()
    for (source,) in retired:
        live = _final_identity(conn, source, cache)
        if live is None or live == source:
            held = conn.execute("SELECT COUNT(*) FROM doc_comments WHERE artifact_id=?", (source,)).fetchone()[0]
            report.left(_DEAD_END, held)
            continue
        _move_comments(conn, source, live, now, cache, report)
        _move_batches(conn, source, live, now, report)
        review_ids = [row[0] for row in conn.execute("SELECT id FROM doc_review_requests WHERE artifact_id=?", (source,))]
        report.moved["doc_review_requests"] += _move_by_ids(conn, "doc_review_requests", review_ids, live, now)
        _move_writer_lines(conn, source, live, now, report)
    return report


def migrate_199_to_200(conn: sqlite3.Connection) -> None:
    """Create the move ledger and move the comment state left on retired identities."""
    conn.execute(COMMENT_IDENTITY_MOVES_SQL.strip().rstrip(";"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"artifact_identities", *MOVED_COLUMNS} <= tables:
        return  # a database whose document tables arrive on demand from the schema fragment holds no such rows
    report = move_comments_to_live_identities(conn)
    logging.getLogger("infra.database_migrations").info("Comment state off retired identities: %s", report.line())


def reverse_moves(conn: sqlite3.Connection) -> dict[str, int]:
    """Put every row the move changed back to the values the ledger recorded; returns counts by outcome.

    A row is restored only while it still sits where the move put it. One someone has changed since stays
    (``changed_since``), one that was deleted drops its ledger row (``gone``), and one whose old values would now
    break a unique index stays (``blocked``). The counts are what the caller reports; the ledger keeps what remains.
    """
    counts = {"reversed": 0, "changed_since": 0, "gone": 0, "blocked": 0}
    reverse_folds(conn, counts)
    entries = conn.execute(f"SELECT table_name, row_id, original, moved_to FROM {LEDGER}").fetchall()
    for table, row_id, original, moved_to in entries:
        if table not in MOVED_COLUMNS:
            continue
        current = conn.execute(f"SELECT artifact_id FROM {table} WHERE id=?", (row_id,)).fetchone()
        if current is None:
            conn.execute(f"DELETE FROM {LEDGER} WHERE table_name=? AND row_id=?", (table, row_id))
            counts["gone"] += 1
            continue
        if current[0] != moved_to:
            counts["changed_since"] += 1
            continue
        values = {key: value for key, value in json.loads(original).items() if key in MOVED_COLUMNS[table]}
        try:
            conn.execute(
                f"UPDATE {table} SET {', '.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), row_id),
            )
        except sqlite3.IntegrityError:
            counts["blocked"] += 1
            continue
        conn.execute(f"DELETE FROM {LEDGER} WHERE table_name=? AND row_id=?", (table, row_id))
        counts["reversed"] += 1
    return counts
