"""The bounded checkpoint-retrieval tool.

One tool, two exclusive selections: the recent generations for a thread, or
one exact generation by id. The exact selection resolves its own owning
thread, so a cross-thread search result never asks the caller to carry a
second identifier correctly.
"""

from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


def _format_hit(hit: dict, *, index: int) -> list[str]:
    """Render a compact current-epoch generation listing."""
    return [
        f"{index}. checkpoint_id: {hit['checkpoint_id']} | "
        f"thread: {hit['thread_id']} | through_seq: {hit['through_seq']} | "
        f"generated: {hit['generated_at'][:16]}",
        f"   {hit['narrative']}" if hit["narrative"] else "   (no narrative)",
    ]


def _format_exact(result: dict) -> str:
    """Render one exact generation, or say why it cannot be read.

    An unavailable read reports the same answer whether the generation is
    missing or simply not the caller's, so this never confirms that another
    user's checkpoint exists.
    """
    if "error_code" in result:
        return f"{result['message']} (error_code={result['error_code']})"
    historical = (
        f" | historical epoch: {result['history_epoch']}"
        if result["historical"] else ""
    )
    header = (
        f"checkpoint_id: {result['checkpoint_id']} | "
        f"thread: {result['thread_id']} | "
        f"through_seq: {result['through_seq']} | "
        f"generated: {result['generated_at'][:16]}{historical}"
    )
    body = result["narrative"] or "(no narrative)"
    tail = "" if result["complete"] else (
        f"\n\n[bytes {result['offset_bytes']}–"
        f"{result['next_offset_bytes']} of {result['total_bytes']}; "
        f"read on with offset_bytes={result['next_offset_bytes']}]"
    )
    entries = "".join(f"\n- {s}" for s in result["continuity"])
    carried = f"\n\nCarried forward:{entries}" if entries else ""
    if result["continuity_unreadable"]:
        carried += (
            "\n\nThis generation's carried-forward entries could not "
            "be read: the stored snapshot is corrupt. The narrative "
            "above is intact, but treat this record as partial."
        )
    return f"{header}\n\n{body}{carried}{tail}"


class GetCheckpointsTool(Tool):
    """List recent checkpoint generations, or read one exactly."""

    @property
    def name(self) -> str:
        return "get_checkpoints"

    @property
    def description(self) -> str:
        return (
            "Read retained checkpoint generations: what a thread had "
            "concluded at an exact point in its history. Search hits can span "
            "caller-owned epochs after project moves; an older epoch is labeled "
            "historical.\n\n"
            "USE THIS when older trajectory matters — an earlier decision, "
            "why an approach was rejected, what a thread had settled before "
            "it was compacted.\n\n"
            "DO NOT use this for:\n"
            "- Current state — use plan_read, the files, or the live thread\n"
            "- Exact wording of something said — use search_history\n"
            "- A fresh calculation — just do it\n\n"
            "A narrative is a historical record, not current authority: cite "
            "it as of its generation time, and do not treat a past proposal "
            "as a present decision.\n\n"
            "Two exclusive selections: pass checkpoint_id from a search hit to "
            "read one full generation (byte-paged), or thread_id to list recent "
            "current-epoch generation summaries."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": (
                        "Read this exact full generation. Use the id from a "
                        "search result verbatim. Mutually exclusive with thread_id."
                    ),
                },
                "thread_id": {
                    "type": "string",
                    "description": (
                        "List recent generations for this thread. Defaults to "
                        "the current thread."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Recent generations to list (default 3)",
                    "minimum": 1,
                    "maximum": 10,
                },
                "offset_bytes": {
                    "type": "integer",
                    "description": "Byte offset into an exact narrative",
                    "minimum": 0,
                },
                "limit_bytes": {
                    "type": "integer",
                    "description": "Bytes to read from an exact narrative",
                    "minimum": 1,
                    "maximum": 20000,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        checkpoint_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 3,
        offset_bytes: int = 0,
        limit_bytes: int = 4000,
        **kwargs: Any,
    ) -> str:
        from infra.database import get_db
        from memory.checkpoint_search import get_checkpoint, recent_checkpoints

        if checkpoint_id and thread_id:
            return (
                "Error: checkpoint_id and thread_id are exclusive. Pass "
                "checkpoint_id to read one generation, or thread_id to list "
                "recent ones."
            )
        # Authorization identity is the backend-injected calling thread, never
        # the caller-supplied thread_id: that argument is a filter, and letting
        # it stand in for identity would let a model name whose data it reads.
        from infra.thread_confinement import Reader

        reader = Reader.from_tool(kwargs)
        if not reader.thread_id:
            return (
                "Error: get_checkpoints could not resolve the calling thread, "
                "so it cannot authorize a read."
            )
        target = thread_id or reader.thread_id
        if not checkpoint_id and not isinstance(target, str):
            return "Error: get_checkpoints requires a thread or a checkpoint_id"

        def _query():
            db = get_db()
            try:
                if checkpoint_id:
                    return get_checkpoint(
                        db,
                        checkpoint_id,
                        reader=reader,
                        offset_bytes=offset_bytes,
                        limit_bytes=limit_bytes,
                    )
                return recent_checkpoints(
                    db, str(target), reader=reader, limit=limit
                )
            finally:
                db.close()

        result = await run_in_db(_query)

        if checkpoint_id:
            return _format_exact(result)

        if not result:
            return "No retained checkpoint generations for this thread."
        lines = [f"{len(result)} retained generation(s), newest first:\n"]
        for index, hit in enumerate(result, start=1):
            lines.extend(_format_hit(hit, index=index))
            lines.append("")
        return "\n".join(lines)
