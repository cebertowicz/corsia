"""Agent-facing tools for declared measure definitions and their intake status."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from infra import config
from infra.database import get_db
from infra.db_async import run_in_db
from infra.measure_registry import sync
from infra.measure_status import status
from tools.base import Tool
from tools.workflow_owners import workflow_owners


def _context(kwargs: dict[str, Any]):
    from tools.delegation import _require_context
    return _require_context(kwargs)


async def _sync(kwargs: dict[str, Any]) -> str:
    context = _context(kwargs)
    if isinstance(context, str):
        return context
    thread_id, actor_row = context
    path = kwargs.get("path")
    if not isinstance(path, str):
        return "Error: path is required"
    try:
        target, text = await asyncio.to_thread(_read_declaration, path)

        def load() -> dict[str, Any]:
            db = get_db()
            try:
                owners = workflow_owners(thread_id, actor_row.get("project_id"), db)
                return sync(db, owner_thread_id=thread_id, path=str(target), text=text, workflows=owners)
            finally:
                db.close()

        return json.dumps(await run_in_db(load), sort_keys=True)
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"


def _read_declaration(path: str) -> tuple[Path, str]:
    """Resolve ``path`` against the workspace and read it, refusing anything but a workspace measures.yaml."""
    target = (config.WORKSPACE / path).resolve()
    if not target.is_relative_to(config.WORKSPACE.resolve()) or target.name != "measures.yaml":
        raise ValueError("path must be a measures.yaml file inside the workspace")
    return target, target.read_text(encoding="utf-8")


class MeasureSyncTool(Tool):
    @property
    def name(self) -> str:
        return "measure_sync"

    @property
    def description(self) -> str:
        return ("Load a measures.yaml into the KPI registry. A workflow's file sits beside its skill's "
                "SKILL.md (<skill dir>/measures.yaml) and syncs only for that workflow's owner: the thread "
                "whose own skill it is, or the `owner:` under `workflow:` in its corsia.yaml. Agent-level "
                "measures go in your own thread folder. `path` is relative to the CORSIA workspace root, so a "
                "project agent passes projects/<project>/.threads/<id>/measures.yaml. Validates names, "
                "attributes, KPIs and views, then returns each item as added, changed, unchanged or removed, "
                "plus a namespace_takeover list when it retired a departed holder's live events. Run it after "
                "every change; nothing takes effect until sync succeeds.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False,
                "properties": {"path": {"type": "string",
                                        "description": "measures.yaml path relative to the CORSIA workspace root"}},
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
                "and refusals by event, plus the active synced definition files. Each recent "
                "refusal names the event, attribute and value, whether the row or only the "
                "attribute was dropped, and the fix. Use it after a script run.")

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
