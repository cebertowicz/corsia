"""Read-only scoped retrieval of canonical tool-result message bytes."""

from __future__ import annotations

import json
from typing import Any

from core.tool_result_projection import (
    MAX_TOOL_RESULT_PAGE_BYTES,
    ToolResultReferenceError,
    resolve_tool_result_page,
)
from infra.database import get_db
from tools.base import Tool


class ReadToolResultTool(Tool):
    """Retrieve one bounded page of a canonical result in the current thread."""

    @property
    def name(self) -> str:
        return "read_tool_result"

    @property
    def description(self) -> str:
        return (
            "Read a byte-bounded page from a referenced canonical tool result "
            "in the current thread."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "minLength": 1},
                "sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                },
                "offset_bytes": {"type": "integer", "minimum": 0},
                "limit_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TOOL_RESULT_PAGE_BYTES,
                },
            },
            "required": [
                "message_id",
                "sha256",
                "offset_bytes",
                "limit_bytes",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        *,
        message_id: str,
        sha256: str,
        offset_bytes: int,
        limit_bytes: int,
        _thread_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        """Resolve the page using backend-injected thread identity only."""
        if not isinstance(_thread_id, str) or not _thread_id:
            return "Error: tool-result retrieval requires current thread identity"
        db = get_db()
        try:
            page = resolve_tool_result_page(
                thread_id=_thread_id,
                message_id=message_id,
                sha256=sha256,
                offset_bytes=offset_bytes,
                limit_bytes=limit_bytes,
                db=db,
            )
        except ToolResultReferenceError as exc:
            return f"Error: {exc}"
        finally:
            db.close()
        return json.dumps(page, sort_keys=True, ensure_ascii=False)
