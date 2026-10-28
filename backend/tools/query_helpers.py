"""Helper functions for query tools — shared FTS5 utilities and DB helpers."""

from __future__ import annotations

# Re-exported for the query tools that already import it from here. The
# definition lives with the index definitions in infra so that memory/ can
# use it without importing back up into the tool layer.
from infra.db_fts import fts5_escape

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
    caller_thread_id: str = "",
) -> list:
    """Search delivered messages by text and durable sender class."""
    sender_expression = _sender_expression()
    fts_query = fts5_escape(query)
    filters = " AND m.delivery_state = 'delivered' AND (? IS NULL OR t.confinement IS NULL OR t.id = ?)"
    params: list = [fts_query, caller_thread_id, caller_thread_id]

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
        like_params: list = [f"%{query}%", caller_thread_id, caller_thread_id]
        like_filters = " AND m.delivery_state = 'delivered' AND (? IS NULL OR t.confinement IS NULL OR t.id = ?)"
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
        "AND delivery_state = 'delivered'",
        (thread_id,),
    ).fetchone()
    return row["cnt"] if row else 0


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
