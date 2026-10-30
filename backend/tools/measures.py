"""Agent-facing tools for declared measure definitions and their intake status."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from infra import config
from infra.database import get_db
from infra.db_async import run_bulk, run_in_db
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


_KPI_PERIOD_FIELDS = ("period", "value", "n", "unknown", "status", "reason", "partial")


def _kpi_summary(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Trim the agent KPI read to each KPI's current and last completed period."""
    def trim(period: dict[str, Any]) -> dict[str, Any]:
        return {key: period[key] for key in _KPI_PERIOD_FIELDS if key in period}

    return [{"name": item["name"], "definition_version": item["definition_version"], "workflow": item["workflow"],
             "current": trim(item["periods"][-1]), "last_complete": trim(item["periods"][-2])}
            for item in result["kpis"]]


async def _own_kpis(user_id: str, project: str | None, thread_id: str) -> list[dict[str, Any]] | str:
    from fastapi import HTTPException

    from api.measurement_agent_kpis import _read
    from api.measurement_scope import resolve_grain

    try:
        weeks = resolve_grain("weeks", "UTC")
        # Only the two periods the summary keeps are read: the last complete week and the current one.
        grain = replace(weeks, periods=weeks.periods[-1:], starts=weeks.starts[-2:])
        return _kpi_summary(await run_bulk(_read, user_id, project or "all", thread_id, grain))
    except HTTPException as exc:
        return f"Error: {exc.detail}"


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
                "plus a namespace_takeover list when it retired a departed holder's live events. For a duration, "
                "an event may declare since: {event: <start event>, on: <shared attribute or map>, "
                "as: <declared num or count attribute>}. Both join attributes must be declared; a map uses "
                "end-to-start names, for example on: {introduced_by: pr}. Intake stamps whole minutes from "
                "the newest matching earlier start, or leaves it absent if none has arrived; later starts do not "
                "backfill it. KPI operands may declare latest or first "
                "with one or two id/enum attributes; these are validated but do not filter results yet. "
                "Run it after every change; nothing takes effect until sync succeeds.")

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
                "attribute was dropped, and the fix. Use it after a script run. With kpis true it also "
                "returns kpis: each KPI you synced, with its current and last complete UTC week (value, n, unknown, status, partial).")

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {"kpis": {"type": "boolean"}}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        context = _context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, actor_row = context
        def read() -> dict[str, Any]:
            return status(get_db(), thread_id)
        result = await run_in_db(read)
        if kwargs.get("kpis"):
            kpis = await _own_kpis(actor_row["user_id"], actor_row.get("project_id"), thread_id)
            if isinstance(kpis, str):
                return kpis
            result["kpis"] = kpis
        return json.dumps(result, sort_keys=True)
