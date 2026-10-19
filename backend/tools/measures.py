"""Agent-facing tools for declared measure definitions and their intake status."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from infra.database import get_db
from infra.db_async import run_in_db
from infra.measure_registry import sync
from infra.measure_status import status
from tools.base import Tool


def _context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context
    return _require_context(kwargs)


async def _sync(kwargs: dict[str, Any]) -> str:
    context = _context(kwargs)
    if isinstance(context, str):
        return context
    thread_id, _ = context
    path = kwargs.get("path")
    if not isinstance(path, str):
        return "Error: path is required"
    try:
        target = Path(path).resolve()
        from infra.config import WORKSPACE
        if not target.is_relative_to(WORKSPACE.resolve()) or target.name != "measures.yaml":
            raise ValueError("path must be a measures.yaml file inside the workspace")
        text = target.read_text(encoding="utf-8")
        result = await run_in_db(partial(sync, owner_thread_id=thread_id, path=str(target), text=text))
        return json.dumps(result, sort_keys=True)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


class MeasureSyncTool(Tool):
    @property
    def name(self) -> str:
        return "measure_sync"

    @property
    def description(self) -> str:
        return ("Load a measures.yaml into the KPI registry. A workflow's file sits beside its skill's "
                "SKILL.md (<skill dir>/measures.yaml), agent-level measures go in "
                ".threads/<agent>/measures.yaml, and only the file's owner can sync it. Validates names, "
                "attributes, KPIs and views, then returns added, changed or unchanged. Run it after every "
                "change; nothing takes effect until sync succeeds.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False,
                "properties": {"path": {"type": "string", "description": "Workspace path to measures.yaml"}},
                "required": ["path"]}

    async def execute(self, **kwargs: Any) -> str:
        return await _sync(kwargs)


class MeasureStatusTool(Tool):
    @property
    def name(self) -> str:
        return "measure_status"

    @property
    def description(self) -> str:
        return ("Show your measure intake for today and the last 7 days: rows recorded, drops "
                "and refusals by event, plus the active synced definition files. Use it after a "
                "script run to find a dropped record and its fix.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        context = _context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, _ = context
        def read() -> dict[str, Any]:
            return status(get_db(), thread_id)
        return json.dumps(await run_in_db(read), sort_keys=True)
