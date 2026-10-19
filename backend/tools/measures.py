"""Agent-facing tools for declared measure definitions and their intake status."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from infra.database import get_db
from infra.db_async import run_in_db
from infra.measure_registry import sync
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
        return ("Load your measures.yaml declaration into the KPI registry. The file must be "
                "inside the workspace and owned by your workflow or agent folder. It validates "
                "names, event attributes, KPI fields and views, then returns added, changed or "
                "unchanged declarations. Run it after every measures.yaml change; declarations "
                "do not take effect until sync succeeds.")

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
            db = get_db()
            rows = db.execute("SELECT event_name, counter, sum(n) FROM outcome_event_counters WHERE agent_thread_id=? AND day >= date('now','-6 days') GROUP BY event_name,counter", (thread_id,)).fetchall()
            definitions = db.execute("SELECT DISTINCT file_path,file_sha256 FROM measure_definitions WHERE owner_thread_id=?", (thread_id,)).fetchall()
            return {"counters": [{"event_name": row[0], "counter": row[1], "n": row[2]} for row in rows], "synced_files": [{"path": row[0], "sha256": row[1]} for row in definitions]}
        return json.dumps(await run_in_db(read), sort_keys=True)
