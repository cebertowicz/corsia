"""The agent-facing read-only model catalog."""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db
from llm.capabilities import snapshot_active_threads
from tools.base import Tool


class ModelCatalogTool(Tool):
    """Return the same live projection as ``GET /api/models``."""

    @property
    def name(self) -> str:
        return "model_catalog"

    @property
    def description(self) -> str:
        return (
            "Read the live model catalog before selecting a delegation target. "
            "Each row names its selector, connection, capabilities and context window. "
            "A row's status is the model's own and says nothing about an account. "
            "For a Claude Code or Codex row, the runs_on entry marked selected is the account a "
            "delegated turn in this project runs on. Runnable summarizes whether the model can "
            "run now: ready when its selected account can run it, or when that account is usage-limited "
            "and another permitted account has room. This does not predict rerouting: read the selected "
            "runs_on entry for the account the turn uses; with rollover off, it stays held on that "
            "limited account. Takes no arguments."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **_kwargs: Any) -> str:
        from llm.delegation_model import caller_dispatch_scope
        from llm.model_catalog_projection import model_catalog_projection

        thread_id = _kwargs.get("_thread_id")
        if thread_id is None:
            payload = await run_in_db(model_catalog_projection, snapshot_active_threads())
        elif not isinstance(thread_id, str) or not thread_id:
            return "Error: model_catalog requires a valid server-side thread context"
        else:
            # A child binds to the project's account, not to the caller's own binding.
            scope = await run_in_db(caller_dispatch_scope, thread_id)
            payload = await run_in_db(model_catalog_projection, snapshot_active_threads(), **scope)
        return json.dumps(payload, sort_keys=True)
