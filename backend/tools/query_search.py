"""Search retained message history and checkpoint generations."""

from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool
from tools.query_helpers import (
    SENDER_CLASSES,
    format_history_results,
    search_messages_fts,
)


def _format_checkpoint_results(formatted: str, query: str, hits: list[dict]) -> str:
    """Render compact checkpoint evidence with an exact-read affordance."""
    lines = [formatted, "", f"Checkpoint evidence matching '{query}':", ""]
    for hit in hits:
        historical = (
            f" | historical epoch: {hit['history_epoch']}"
            if hit["historical"] else ""
        )
        if hit["kind"] == "continuity_entry":
            entry = f"entry_id: {hit['entry_id']} | field: {hit['field']}"
            first = hit["first_checkpoint"] or {"checkpoint_id": "unavailable", "generated_at": ""}
            latest = hit["latest_checkpoint"] or {"checkpoint_id": "unavailable", "generated_at": ""}
            lifecycle_detail = ""
            if hit["entry_state"] == "revised":
                lifecycle_detail = f" | current revision: {hit['current_revision']}"
            elif hit["entry_state"] == "retired":
                lifecycle_detail = f" | retired reason: {hit['retired_reason']}"
            lines.append(
                f"{entry} | thread: {hit['thread_id']} | "
                f"state: {hit['entry_state']}{lifecycle_detail} | "
                f"first: {first['checkpoint_id']} "
                f"({first['generated_at'][:16]}) | "
                f"latest: {latest['checkpoint_id']} "
                f"({latest['generated_at'][:16]})"
                f"{historical}"
            )
            lines.append(hit["statement"])
        else:
            unreadable = (
                " | continuity: unreadable"
                if hit.get("continuity_unreadable")
                else ""
            )
            lines.append(
                f"checkpoint_id: {hit['checkpoint_id']} | "
                f"thread: {hit['thread_id']} | "
                f"generated: {hit['generated_at'][:16]}{unreadable}{historical}"
            )
            lines.append(hit["narrative"] or "(no narrative)")
        lines.append("")
    lines.append("Read the full generation exactly with get_checkpoints(checkpoint_id=...).")
    return "\n".join(lines)


class SearchHistoryTool(Tool):
    """Search message history across threads or within a specific thread."""

    @property
    def name(self) -> str:
        return "search_history"

    @property
    def description(self) -> str:
        return (
            "Search message history across threads or within a specific thread.\n\n"
            "Uses FTS5 keyword matching — every query word must appear in the text.\n"
            "Use 1-3 precise keywords that would actually appear in the target text.\n"
            "Think like the author, not the searcher. No filler words.\n\n"
            "USE THIS when you need to:\n"
            "- Find what was discussed about a topic across threads\n"
            "- Recall a specific thread or decision\n"
            "- Look up what a human, agent, or system source said on a topic\n"
            "- Check if something was already discussed\n\n"
            "Supports filtering by sender, thread, and keyword. Sender values: "
            f"{', '.join(SENDER_CLASSES)}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "1-3 keywords likely to appear in the target text. Every word is an AND filter — fewer precise words beat longer phrases."
                },
                "thread_id": {
                    "type": "string",
                    "description": "Limit search to a specific thread ID (optional)"
                },
                "sender": {
                    "type": "string",
                    "enum": list(SENDER_CLASSES),
                    "description": "Filter by durable sender class (optional)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 20)",
                    "minimum": 1,
                    "maximum": 50
                },
                "include_checkpoints": {
                    "type": "boolean",
                    "description": (
                        "Also search retained checkpoint entries and narratives (default "
                        "false). Use when older trajectory matters — what a "
                        "thread had concluded, and why — rather than the exact "
                        "wording of a message. Search spans the caller's accessible "
                        "epochs after project moves; results are historical evidence, "
                        "not current authority. Use plan_read for the current plan."
                    )
                },
            },
            "required": ["query"]
        }

    async def execute(
        self,
        query: str,
        thread_id: str | None = None,
        sender: str | None = None,
        limit: int = 20,
        include_checkpoints: bool = False,
        **kwargs: Any,
    ) -> str:
        from infra.database import get_db
        from memory.checkpoint_search import search_checkpoints

        if not query or not query.strip():
            return "Error: search_history requires a non-empty query. Provide 1-3 precise keywords."

        # Checkpoint retrieval authorizes against the backend-injected calling
        # thread; without one it returns nothing rather than reading unscoped.
        # That omission is reported below rather than silently dropped: a
        # requested result source that cannot be searched must not look like a
        # source that was searched and found nothing.
        caller = kwargs.get("_thread_id")
        caller_resolved = isinstance(caller, str) and bool(caller)
        search_checkpoints_here = include_checkpoints and caller_resolved

        def _query():
            db = get_db()
            rows = search_messages_fts(
                db, query, thread_id=thread_id, sender=sender, limit=limit,
                caller_thread_id=str(caller) if caller_resolved else "",
            )
            cp_rows = (
                search_checkpoints(
                    db,
                    query,
                    caller_thread_id=str(caller),
                    thread_id=thread_id,
                    limit=limit,
                )
                if search_checkpoints_here
                else None
            )
            db.close()
            return rows, cp_rows

        rows, cp_rows = await run_in_db(_query)
        formatted = format_history_results(rows, query, thread_id, sender)
        if include_checkpoints and not caller_resolved:
            return (
                f"{formatted}\n\nCheckpoint generations were NOT searched: "
                "this call has no resolved calling thread, so a checkpoint "
                "read cannot be authorized. The message results above are "
                "complete; the checkpoint half of this search is missing, "
                "not empty."
            )
        if not cp_rows:
            return formatted
        return _format_checkpoint_results(formatted, query, cp_rows)
