"""Helper functions for query tools — shared FTS5 utilities and DB helpers."""

from __future__ import annotations

# Re-exported for the query tools that already import it from here. The
# definition lives with the index definitions in infra so that memory/ can
# use it without importing back up into the tool layer.
from infra.db_fts import fts5_escape
from infra.project_fence import HistoryFence
from infra.message_update_schema import not_replaced_sql
from infra.thread_confinement import Reader, hidden_clause

__all__ = ["fts5_escape"]


SENDER_CLASSES = (
    "human",
    "agent_send",
    "subagent_result",
    "assistant",
    "tool",
    "routed_event",
    "plan_wake",
    "system_digest",
    "system",
)

def _sender_expression() -> str:
    """Return the SQLite expression that names one durable message sender class."""
    valid = "json_valid(m.metadata)"
    return (
        "CASE "
        f"WHEN m.role = 'user' AND {valid} "
        "AND json_extract(m.metadata, '$.sender_kind') = 'agent' "
        "AND json_extract(m.metadata, '$.completion') = 1 THEN 'subagent_result' "
        f"WHEN m.role = 'user' AND {valid} "
        "AND json_extract(m.metadata, '$.sender_kind') = 'agent' THEN 'agent_send' "
        f"WHEN m.role = 'system' AND {valid} "
        "AND json_extract(m.metadata, '$.routed_event') IS NOT NULL THEN 'routed_event' "
        f"WHEN m.role = 'system' AND {valid} "
        "AND json_extract(m.metadata, '$.type') = 'context_event' "
        "AND json_extract(m.metadata, '$.event_disposition') = 'wake' "
        "AND json_extract(m.metadata, '$.event_type') GLOB 'plan.*' THEN 'plan_wake' "
        f"WHEN m.role = 'system' AND {valid} "
        "AND json_extract(m.metadata, '$.type') = 'context_event' "
        "AND json_extract(m.metadata, '$.event_disposition') = 'passive' "
        "THEN 'system_digest' "
        "WHEN m.role = 'user' THEN 'human' "
        "ELSE m.role END"
    )


def search_messages_fts(
    db,
    query: str,
    thread_id: str | None = None,
    sender: str | None = None,
    limit: int = 20,
    reader: Reader = Reader.agent(),
    fence: HistoryFence | None = None,
) -> list:
    """Search delivered messages by text and durable sender class.

    A message of a thread whose current project the reader is fenced from, or at or
    below that thread's cut, is dropped in SQL so it never counts toward ``limit``.
    Pass the call's ``fence`` to read several sources under one computed fence.
    """
    sender_expression = _sender_expression()
    fts_query = fts5_escape(query)
    visible, identity = hidden_clause(reader, "t")
    open_to_caller, fence_params = (fence or HistoryFence.for_reader(reader, db=db)).visible_clause("t", "m.seq")
    filters = f" AND m.delivery_state = 'delivered' AND {visible} AND {open_to_caller} AND {not_replaced_sql('m.id')}"
    params: list = [fts_query, *identity, *fence_params]

    if thread_id:
        filters += " AND m.thread_id = ?"
        params.append(thread_id)
    if sender:
        filters += f" AND ({sender_expression}) = ?"
        params.append(sender)
    params.append(limit)

    try:
        sql = (
            "SELECT m.id, m.thread_id, m.role, m.content, m.created_at, t.title, "
            f"{sender_expression} AS sender, bm25(messages_fts) as bm25_score "
            "FROM messages m "
            "JOIN messages_fts f ON f.rowid = m.rowid "
            "JOIN threads t ON t.id = m.thread_id "
            f"WHERE messages_fts MATCH ?{filters} "
            "ORDER BY bm25(messages_fts) LIMIT ?"
        )
        return db.execute(sql, params).fetchall()
    except Exception:
        like_params: list = [f"%{query}%", *identity, *fence_params]
        like_filters = f" AND m.delivery_state = 'delivered' AND {visible} AND {open_to_caller} AND {not_replaced_sql('m.id')}"
        if thread_id:
            like_filters += " AND m.thread_id = ?"
            like_params.append(thread_id)
        if sender:
            like_filters += f" AND ({sender_expression}) = ?"
            like_params.append(sender)
        like_params.append(limit)
        sql = (
            "SELECT m.id, m.thread_id, m.role, m.content, m.created_at, t.title, "
            f"{sender_expression} AS sender, NULL as bm25_score "
            "FROM messages m "
            "JOIN threads t ON t.id = m.thread_id "
            f"WHERE m.content LIKE ?{like_filters} "
            "ORDER BY m.created_at DESC LIMIT ?"
        )
        return db.execute(sql, like_params).fetchall()


def get_thread_message_count(db, thread_id: str) -> int:
    """Return the number of delivered messages in a thread."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE thread_id = ? "
        f"AND delivery_state = 'delivered' AND {not_replaced_sql()}",
        (thread_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def thread_history_rows(
    db,
    thread_id: str,
    *,
    reader: Reader,
    role: str | None = None,
    limit: int = 50,
    oldest_first: bool = False,
    from_seq: int | None = None,
    to_seq: int | None = None,
    tool_output: str = "preview",
) -> tuple[dict | None, object]:
    """One thread's readable message rows for this caller: ``(thread_row, rows)``.

    The project wall is applied here, in SQL, so a fenced row is never fetched and
    never counts toward ``limit``. The second half is the rows, or a
    ``RestrictedRead`` for a confined thread, or the row R5 refusal text for a thread
    wholly behind a wall the caller is outside: three answers, and the caller renders
    whichever one arrived. A thread that left a Local only project serves only the
    messages above its cut.
    """
    from infra.project_fence import thread_refusal
    from infra.thread_confinement import transcript_restriction

    thread_row = db.execute(
        "SELECT id, title, confinement, local_only_cut_seq FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    if not thread_row:
        return None, None
    restriction = transcript_restriction(reader, thread_row)
    if restriction is not None:
        return thread_row, restriction
    fence = HistoryFence.for_reader(reader, db=db)
    subject = fence.wholly_fenced(db, thread_id)
    if subject is not None:
        return thread_row, thread_refusal(subject, thread_id, caller=reader.thread_id)
    where = ["thread_id = ?", "delivery_state = 'delivered'", "seq IS NOT NULL"]
    params: list[object] = [thread_id]
    hidden = fence.hidden_below(thread_row["local_only_cut_seq"])
    if hidden is not None:
        where.append("seq > ?")
        params.append(hidden)
    if role:
        where.append("role = ?")
        params.append(role)
    if from_seq is not None:
        where.append("seq >= ?")
        params.append(from_seq)
    if to_seq is not None:
        where.append("seq <= ?")
        params.append(to_seq)
    if tool_output == "omit":
        where.append("role <> 'tool'")
    if from_seq is None or from_seq != to_seq:
        where.append(not_replaced_sql())
    order = "ASC" if oldest_first else "DESC"
    rows = db.execute(
        f"SELECT id, seq, role, content, metadata, created_at FROM messages "
        f"WHERE {' AND '.join(where)} ORDER BY seq {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    from tools.post_update_history import history_updates

    return thread_row, history_updates(db, rows)


def format_history_results(
    rows: list,
    query: str,
    thread_id: str | None,
    sender: str | None = None,
) -> str:
    """Format search_history results into a readable string.

    Args:
        rows: Message rows from search_messages_fts.
        query: Original search query (for header).
        thread_id: Optional thread filter (for header).
    """
    results = []

    for r in rows:
        content_preview = (r["content"] or "")[:500]
        thread_title = r["title"] or "Untitled"
        bm25 = r["bm25_score"]
        score_str = f" [score: {bm25:.4f}]" if bm25 is not None else ""
        results.append(
            f"[{r['created_at'][:16]}] {r['sender'].upper().replace('_', ' ')} | "
            f"thread_id: {r['thread_id']} | \"{thread_title}\"{score_str}:\n"
            f"  {content_preview}"
        )

    if not results:
        scope = f" in thread {thread_id}" if thread_id else ""
        source = f" from sender {sender}" if sender else ""
        return f"No messages found matching '{query}'{scope}{source}"

    header = f"Found {len(rows)} message(s)"
    header += f" matching '{query}'"
    if thread_id:
        header += f" in thread {thread_id}"
    if sender:
        header += f" from sender {sender}"

    parts = [header + ":\n"]
    if results:
        parts.append("\n\n".join(results))
    return "\n".join(parts)


def list_projects_query(db, status: str = "active") -> list:
    """Fetch projects from DB filtered by status."""
    if status == "all":
        return db.execute("SELECT * FROM projects ORDER BY name").fetchall()
    return db.execute(
        "SELECT * FROM projects WHERE status = ? ORDER BY name", (status,)
    ).fetchall()


def format_projects_list(rows: list, status: str) -> str:
    """Format a list of project rows into a readable string."""
    if not rows:
        return f"No {status} projects found"
    lines = [f"{len(rows)} project(s):\n"]
    for r in rows:
        path_info = f" → {r['path']}" if r["path"] else ""
        desc = f"\n  {r['description']}" if r["description"] else ""
        conductor = r["conductor_thread_id"] or "none (no designated conductor)"
        lines.append(
            f"- **{r['name']}** [{r['status']}] (id: {r['id']}){path_info}{desc}"
            f"\n  conductor_thread_id: {conductor}"
        )
    return "\n".join(lines)
