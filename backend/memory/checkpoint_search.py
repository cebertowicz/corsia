"""Bounded retrieval over retained checkpoint generations.

Search returns one matching frozen continuity entry or a compact narrative result.
Exact retrieval is the separate path that returns a whole frozen generation.
"""

from __future__ import annotations

import sqlite3

from loguru import logger as log

from infra.db_fts import fts5_escape
from infra.thread_confinement import Reader, hidden_clause
from memory.checkpoint_entry_rows import entry_hits as _entry_hits
from memory.checkpoint_entry_rows import entry_rows_matching
from memory.checkpoint_search_entries import (
    build_narrative_hit,
    continuity_entries,
    generation_summary,
)

DEFAULT_PAGE_BYTES = 4_000
MAX_CANDIDATE_ENTRIES = 400

UNAVAILABLE_MESSAGE = (
    "Checkpoint unavailable. Verify the ID against the originating search "
    "result. This response does not distinguish missing from inaccessible "
    "content."
)


def caller_user_id(db: sqlite3.Connection, caller_thread_id: str | None) -> str | None:
    """Resolve the calling thread's owner, failing closed when unknown."""
    if not caller_thread_id:
        return None
    row = db.execute(
        "SELECT user_id FROM threads WHERE id = ?", (caller_thread_id,)
    ).fetchone()
    return None if row is None else row["user_id"]


def _thread_is_owned(
    db: sqlite3.Connection, thread_id: str, user_id: str
) -> bool:
    """Return whether an optional thread filter belongs to the caller's owner."""
    return db.execute(
        "SELECT 1 FROM threads WHERE id = ? AND user_id = ?", (thread_id, user_id)
    ).fetchone() is not None


def _log_search(reason: str, hits: list[dict]) -> None:
    """Record one observable retrieval outcome at the search decision point."""
    historical = sum(1 for hit in hits if hit.get("historical"))
    log.info(
        "checkpoint_search reason={} hits={} current_hits={} historical_hits={}",
        reason,
        len(hits),
        len(hits) - historical,
        historical,
    )


def _entry_rows(
    db: sqlite3.Connection,
    query: str,
    user_id: str,
    thread_id: str | None,
    candidate_limit: int,
    per_thread: int,
    reader: Reader,
) -> list[dict]:
    """Return bounded entry-FTS matches with lifecycle and checkpoint pointers."""
    return entry_rows_matching(
        db, fts5_escape(query, prefix=True), user_id, thread_id,
        candidate_limit, per_thread, reader,
    )


def _narrative_rows(
    db: sqlite3.Connection,
    query: str,
    user_id: str,
    thread_id: str | None,
    candidate_limit: int,
    per_thread: int,
    reader: Reader,
) -> list[dict]:
    """Return bounded narrative-only FTS matches without rebuilding entries."""
    params: list[object] = [f"raw_result : ({fts5_escape(query, prefix=True)})"]
    scope = ""
    if thread_id is not None:
        scope = "AND checkpoint.thread_id = ? "
        params.append(thread_id)
    visible, identity = hidden_clause(reader, "thread")
    params.extend((user_id, *identity, max(1, per_thread) * 20, candidate_limit))
    rows = db.execute(
        "WITH scored AS ("
        "  SELECT checkpoint.id, checkpoint.thread_id, checkpoint.through_seq, "
        "         checkpoint.installed_at, checkpoint.captured_at, checkpoint.raw_result, "
        "         checkpoint.continuity_snapshot_json, checkpoint.history_epoch, "
        "         checkpoint.generation_order, "
        "         CASE WHEN checkpoint.history_epoch != thread.history_epoch THEN 1 ELSE 0 END "
        "             AS historical, bm25(checkpoints_fts) AS relevance "
        "  FROM checkpoints_fts "
        "  JOIN checkpoints AS checkpoint "
        "    ON checkpoint.generation_order = checkpoints_fts.rowid "
        "  JOIN threads AS thread ON thread.id = checkpoint.thread_id "
        "  WHERE checkpoints_fts MATCH ? "
        f"    {scope}"
        "    AND checkpoint.status = 'installed' AND thread.user_id = ?"
        f"    AND {visible}"
        "), ranked AS ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY thread_id "
        "      ORDER BY relevance ASC, generation_order DESC) AS thread_rank "
        "  FROM scored"
        ") SELECT * FROM ranked WHERE thread_rank <= ? "
        "ORDER BY relevance ASC, generation_order DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _narrative_hits(rows: list[dict]) -> list[tuple[float, int, dict]]:
    """Shape narrative candidates without loading whole continuity histories."""
    hits: list[tuple[float, int, dict]] = []
    for row in rows:
        _, unreadable = continuity_entries(row["continuity_snapshot_json"])
        hits.append((
            float(row["relevance"]),
            int(row["generation_order"]),
            build_narrative_hit(row, unreadable=unreadable),
        ))
    return hits


def search_checkpoints(
    db: sqlite3.Connection,
    query: str,
    *,
    reader: Reader,
    thread_id: str | None = None,
    limit: int = 5,
    per_thread: int = 1,
) -> list[dict]:
    """Search installed epochs in the caller's threads for concise history evidence.

    Older epochs remain readable after a project move, but their hits are marked
    ``historical``. The current-plan authority remains the plan, not a result
    from this historical retrieval surface. Prefix recall lets ``poll`` find
    ``polling``. Infix matching (``poll`` finding ``extrapolling``) is a
    recorded non-decision: it would need a full scan or a trigram index, so it
    is deliberately not supported and should not be rebuilt without a new case
    for it. ``per_thread`` keeps a cumulative long thread, which repeats the
    same topic in every generation, from crowding out other owned threads.
    """
    if not (query or "").strip():
        _log_search("no_terms", [])
        return []
    user_id = caller_user_id(db, reader.thread_id)
    if user_id is None:
        _log_search("unresolved_caller", [])
        return []
    if thread_id and not _thread_is_owned(db, thread_id, user_id):
        # The result remains empty for either a foreign or a missing thread;
        # only the operator outcome explains why this search did not run.
        _log_search("unavailable_thread", [])
        return []
    bounded = max(1, min(int(limit or 5), 20))
    keep = max(1, int(per_thread or 1))
    candidate_limit = min(
        MAX_CANDIDATE_ENTRIES, max(50, bounded * keep * 20)
    )
    entry_hits = _entry_hits(
        _entry_rows(db, query, user_id, thread_id, candidate_limit, keep, reader)
    )
    narrative_hits = _narrative_hits(
        _narrative_rows(db, query, user_id, thread_id, candidate_limit, keep, reader)
    )
    hits: list[dict] = []
    per_thread_count: dict[str, int] = {}
    # bm25 is lower for a better match; equal relevance prefers the newer
    # generation. Entry and narrative hits compete on that one ordering.
    ranked = sorted(
        (*entry_hits, *narrative_hits), key=lambda item: (item[0], -item[1])
    )
    for _relevance, _generation_order, hit in ranked:
        current_count = per_thread_count.get(hit["thread_id"], 0)
        if current_count >= keep:
            continue
        hits.append(hit)
        per_thread_count[hit["thread_id"]] = current_count + 1
        if len(hits) == bounded:
            break
    if not hits:
        reason = "no_hits"
    elif any(hit["historical"] for hit in hits):
        reason = "hits_include_historical"
    else:
        reason = "hits_current_only"
    _log_search(reason, hits)
    return hits


def recent_checkpoints(
    db: sqlite3.Connection,
    thread_id: str,
    *,
    reader: Reader,
    limit: int = 3,
) -> list[dict]:
    """Return a thread's newest installed current-epoch generation summaries."""
    user_id = caller_user_id(db, reader.thread_id)
    if user_id is None:
        return []
    visible, identity = hidden_clause(reader, "t")
    bounded = max(1, min(int(limit or 3), 10))
    rows = db.execute(
        "SELECT c.id, c.thread_id, c.through_seq, c.installed_at, c.captured_at, "
        "       c.raw_result, c.history_epoch, 0 AS historical FROM checkpoints c "
        "JOIN threads t ON t.id = c.thread_id "
        "WHERE c.thread_id = ? AND c.status = 'installed' "
        f"  AND c.history_epoch = t.history_epoch AND t.user_id = ? AND {visible} "
        "ORDER BY c.generation_order DESC LIMIT ?",
        (thread_id, user_id, *identity, bounded),
    ).fetchall()
    return [generation_summary(dict(row)) for row in rows]


def get_checkpoint(
    db: sqlite3.Connection,
    checkpoint_id: str,
    *,
    reader: Reader,
    offset_bytes: int = 0,
    limit_bytes: int = DEFAULT_PAGE_BYTES,
) -> dict:
    """Read one exact installed generation, including its full continuity snapshot.

    Ownership still resolves from the backend-injected caller. An unavailable
    read deliberately remains indistinguishable from a missing id.
    """
    user_id = caller_user_id(db, reader.thread_id)
    visible, identity = hidden_clause(reader, "t")
    row = None if user_id is None else db.execute(
        "SELECT c.id, c.thread_id, c.through_seq, c.installed_at, c.captured_at, "
        "       c.raw_result, c.continuity_snapshot_json, c.history_epoch, "
        "       CASE WHEN c.history_epoch != t.history_epoch THEN 1 ELSE 0 END "
        "           AS historical FROM checkpoints c "
        "JOIN threads t ON t.id = c.thread_id "
        f"WHERE c.id = ? AND c.status = 'installed' AND t.user_id = ? AND {visible}",
        (checkpoint_id, user_id, *identity),
    ).fetchone()
    if row is None:
        return {
            "error_code": "not_found",
            "next_action": "verify_source_result",
            "message": UNAVAILABLE_MESSAGE,
        }

    item = dict(row)
    narrative = str(item["raw_result"] or "")
    # The contract says bytes, so page over encoded bytes rather than
    # characters: a narrative with any non-ASCII text would otherwise report
    # offsets a caller cannot advance by without skipping or repeating text.
    encoded = narrative.encode("utf-8")
    start = max(0, int(offset_bytes or 0))
    size = max(1, int(limit_bytes or DEFAULT_PAGE_BYTES))
    # A page boundary can land inside a multi-byte character; drop the partial
    # tail rather than emitting a replacement character.
    page = encoded[start : start + size].decode("utf-8", errors="ignore")
    # Byte-honest resume point: recompute the consumed bytes from the decoded
    # page. Deriving the next offset from len(narrative) is a character count
    # and would skip or repeat content on a non-ASCII narrative.
    consumed = len(page.encode("utf-8"))
    entries, unreadable = continuity_entries(item["continuity_snapshot_json"])
    return {
        "checkpoint_id": item["id"],
        "thread_id": item["thread_id"],
        "through_seq": item["through_seq"],
        "generated_at": item["installed_at"] or item["captured_at"] or "",
        "history_epoch": item["history_epoch"],
        "historical": bool(item["historical"]),
        "narrative": page,
        "continuity": [
            entry.primary_text for entry in entries if entry.primary_text is not None
        ],
        "continuity_unreadable": unreadable,
        "offset_bytes": start,
        "next_offset_bytes": start + consumed,
        "total_bytes": len(encoded),
        "complete": start + consumed >= len(encoded),
    }
