from __future__ import annotations

import json
from typing import Any

from tools.base import Tool


NULL_TOOL_NAME = "null_probe"


class NullProbeTool(Tool):

    @property
    def name(self) -> str:
        return NULL_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Returns immediately with a fixed payload. "
            "Used by the CORSIA eval harness to measure tool-loop seam "
            "overhead independently of inference time. Call with a unique "
            "probe_id when prompted."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "probe_id": {
                    "type": "string",
                    "description": "Unique identifier for this probe invocation.",
                },
            },
            "required": ["probe_id"],
        }

    async def execute(self, **kwargs: Any) -> str:
        for k in ("_thread_id", "_thread_project_id"):
            kwargs.pop(k, None)
        probe_id = kwargs.get("probe_id", "")
        return json.dumps({"ok": True, "probe_id_echo": probe_id})


__all__ = ["NullProbeTool", "NULL_TOOL_NAME"]
