"""Entry-level FTS reads over the checkpoint entry index.

Ranking runs on lean index rows; large frozen snapshots are joined and decoded
only for bounded results.
"""

from __future__ import annotations

import sqlite3

from loguru import logger as log

from core.continuity_render import ContinuitySnapshotEntry
from infra.checkpoint_entry_index import NO_REVISION
from memory.checkpoint_search_entries import build_entry_hit, continuity_entries


def scope_clause(thread_id: str | None, params: list[object]) -> str:
    """Append an optional owned-thread filter to a checkpoint search query."""
    if thread_id is None:
        return ""
    params.append(thread_id)
    return "AND entry.thread_id = ? "



def entry_rows_matching(
    db: sqlite3.Connection,
    match: str,
    user_id: str,
    thread_id: str | None,
    candidate_limit: int,
    per_thread: int,
) -> list[dict]:
    """Return entry rows for an already-built FTS5 ``match`` expression."""
    return hydrate_entry_rows(
        db,
        entry_rank_rows_matching(
            db, match, user_id, thread_id, candidate_limit, per_thread
        ),
    )


def entry_rank_rows_matching(
    db: sqlite3.Connection,
    match: str,
    user_id: str,
    thread_id: str | None,
    candidate_limit: int,
    per_thread: int,
) -> list[dict]:
    """Return the ranked, bounded entry rows with no checkpoint columns.

    Everything ranking and ``select`` need lives on the lean index and
    continuity rows; :func:`hydrate_entry_rows` adds the checkpoint pointers and
    the large frozen snapshots for whichever rows survive.
    """
    params: list[object] = [match]
    scope = scope_clause(thread_id, params)
    params.extend((user_id, max(1, per_thread) * 20, candidate_limit))
    # Both window sorts carry only the narrow ranking keys of every match; the
    # wide columns (search_text, canonical body) are joined back for the bounded
    # winners. Carrying them through the sorts cost more than the FTS match.
    rows = db.execute(
        "WITH scored AS ("
        "  SELECT entry.rowid AS entry_rowid, entry.thread_id, entry.history_epoch, "
        "         entry.entry_id, entry.last_generation_order, "
        "         bm25(checkpoint_entry_search_fts) AS relevance "
        "  FROM checkpoint_entry_search_fts "
        "  JOIN checkpoint_entry_search AS entry "
        "    ON entry.rowid = checkpoint_entry_search_fts.rowid "
        "  JOIN threads AS thread ON thread.id = entry.thread_id "
        "  WHERE checkpoint_entry_search_fts MATCH ? "
        f"    {scope}"
        "    AND thread.user_id = ?"
        "), revisions AS ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "      PARTITION BY thread_id, history_epoch, entry_id "
        "      ORDER BY relevance ASC, last_generation_order DESC"
        "  ) AS entry_rank FROM scored"
        "), ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY thread_id "
        "      ORDER BY relevance ASC, last_generation_order DESC) AS thread_rank "
        "  FROM revisions WHERE entry_rank = 1"
        "), top AS ("
        "  SELECT * FROM ranked WHERE thread_rank <= ? "
        "  ORDER BY relevance ASC, last_generation_order DESC LIMIT ?"
        ") SELECT entry.thread_id, entry.history_epoch, entry.entry_id, "
        "       entry.revision, entry.field, entry.first_generation_order, "
        "       entry.last_generation_order, entry.search_text, "
        "       CASE WHEN entry.history_epoch != thread.history_epoch THEN 1 ELSE 0 END "
        "           AS historical, canonical.field AS canonical_field, "
        "       canonical.body AS canonical_body, canonical.revision AS canonical_revision, "
        "       canonical.state AS canonical_state, "
        "       canonical.retired_reason AS canonical_retired_reason, "
        "       top.relevance, top.entry_rank, top.thread_rank "
        "FROM top "
        "JOIN checkpoint_entry_search AS entry ON entry.rowid = top.entry_rowid "
        "JOIN threads AS thread ON thread.id = entry.thread_id "
        "LEFT JOIN continuity_entries AS canonical "
        "  ON canonical.thread_id = entry.thread_id "
        " AND canonical.history_epoch = entry.history_epoch "
        " AND canonical.entry_id = entry.entry_id "
        "ORDER BY top.relevance ASC, top.last_generation_order DESC",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def hydrate_entry_rows(db: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Add checkpoint pointers, ``raw_result`` and the frozen snapshot to ``rows``.

    Input order is kept. Snapshots are read once per distinct first checkpoint;
    a row whose checkpoint is gone is dropped, as the former inner join did.
    """
    if not rows:
        return []
    firsts = sorted({int(row["first_generation_order"]) for row in rows})
    orders = sorted(firsts + [int(row["last_generation_order"]) for row in rows])
    pointers = {
        int(got["generation_order"]): got
        for got in _checkpoint_rows(
            db,
            "generation_order, id, COALESCE(installed_at, captured_at) AS generated_at",
            orders,
        )
    }
    heavy = {
        int(got["generation_order"]): got
        for got in _checkpoint_rows(
            db, "generation_order, through_seq, raw_result, continuity_snapshot_json",
            firsts,
        )
    }
    hydrated = []
    for row in rows:
        first = pointers.get(int(row["first_generation_order"]))
        latest = pointers.get(int(row["last_generation_order"]))
        body = heavy.get(int(row["first_generation_order"]))
        if first is None or latest is None or body is None:
            continue
        hydrated.append({
            **row,
            "id": first["id"],
            "first_id": first["id"],
            "first_generated_at": first["generated_at"],
            "latest_id": latest["id"],
            "latest_generated_at": latest["generated_at"],
            "through_seq": body["through_seq"],
            "raw_result": body["raw_result"],
            "continuity_snapshot_json": body["continuity_snapshot_json"],
        })
    return hydrated


def _checkpoint_rows(
    db: sqlite3.Connection, columns: str, orders: list[int]
) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in orders)
    return db.execute(
        f"SELECT {columns} FROM checkpoints WHERE generation_order IN ({marks})",
        orders,
    ).fetchall()


def entry_hits(rows: list[dict]) -> list[tuple[float, int, dict]]:
    """Decode only selected frozen snapshots using the shared entry type."""
    hits: list[tuple[float, int, dict]] = []
    for row, entry in decoded_entries(rows):
        canonical = None
        if row["canonical_state"] is not None:
            canonical = {
                "field": row["canonical_field"],
                "body": row["canonical_body"],
                "revision": row["canonical_revision"],
                "state": row["canonical_state"],
                "retired_reason": row["canonical_retired_reason"],
            }
        pointers = {
            "first_id": row["first_id"],
            "first_generated_at": row["first_generated_at"],
            "latest_id": row["latest_id"],
            "latest_generated_at": row["latest_generated_at"],
        }
        hits.append((
            float(row["relevance"]),
            int(row["last_generation_order"]),
            build_entry_hit(row, entry, canonical, pointers),
        ))
    return hits


def decoded_entries(rows: list[dict]) -> list[tuple[dict, ContinuitySnapshotEntry]]:
    """Pair hydrated rows with decoded snapshot entries, logging stale rows."""
    snapshots: dict[int, tuple[list[ContinuitySnapshotEntry], bool]] = {}
    decoded: list[tuple[dict, ContinuitySnapshotEntry]] = []
    for row in rows:
        first_order = int(row["first_generation_order"])
        entries, unreadable = snapshots.setdefault(
            first_order, continuity_entries(row["continuity_snapshot_json"])
        )
        entry = next(
            (
                candidate
                for candidate in entries
                if candidate.entry_id == row["entry_id"]
                and (candidate.revision or NO_REVISION) == row["revision"]
                and candidate.field == row["field"]
            ),
            None,
        )
        if entry is None:
            log.warning(
                "checkpoint_search reason=stale_entry_index_row "
                "thread_id={} entry_id={} revision={} checkpoint_order={} "
                "snapshot_unreadable={}",
                row["thread_id"], row["entry_id"], row["revision"],
                first_order, unreadable,
            )
            continue
        decoded.append((row, entry))
    return decoded
