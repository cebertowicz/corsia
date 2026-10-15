"""Stable responsibility map and on-demand Agent roster."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.agent_roster_queries import agent_rows
from core.effective_context import ContextSourceEntry
from core.installed_context import RESPONSIBILITY_MAP_SNAPSHOT, installed_map
from tools.base import Tool


def _json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [raw]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    return []


def _self_line(current: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    responsibility = str(current.get("agent_responsibility") or "").strip()
    registered = any(row["id"] == current["id"] for row in rows)
    if registered and responsibility:
        title = current.get("title") or "Untitled"
        return f"Self: {title} ({current['id']}) — {responsibility}"
    if current.get("spawned_by"):
        return f"Self: this thread ({current['id']}) is a temporary worker."
    return f"Self: this thread ({current['id']}) has no registered responsibility."


def _conductor_gap_line(current: dict[str, Any]) -> list[str]:
    gap = current.get("corsia_agent_gap")
    return [f"CORSIA agent: not reachable ({gap})."] if gap else []


def _owner_map_content(current: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = ["[Agent responsibility map]", _self_line(current, rows)]
    others = [row for row in rows if row["id"] != current["id"]]
    if others:
        has_workspace_contact = current.get("project_id") is not None and any(
            row["project_id"] is None for row in others
        )
        lines.append(
            "Other agents and the CORSIA agent:"
            if has_workspace_contact else "Other agents in scope:"
        )
        for row in others:
            scope = "workspace" if row["project_id"] is None else "project"
            lines.append(
                f"- {row.get('title') or 'Untitled'} ({row['id']}) "
                f"[{scope}] — {row['agent_responsibility']}"
            )
    lines.extend(_conductor_gap_line(current))
    lines.append("Use agent_roster for paths, domains, and coordination notes.")
    return "\n".join(lines)


def _execution_epoch(current: dict[str, Any], model: str) -> int:
    pending_epoch = current.get("pending_execution_epoch")
    if current.get("pending_model") == model and isinstance(pending_epoch, int):
        return pending_epoch
    return int(current.get("execution_epoch") or 0)


def _map_snapshot(
    current: dict[str, Any], rows: list[dict[str, Any]], *, model: str,
) -> dict[str, Any]:
    return {
        "model": model,
        "execution_epoch": _execution_epoch(current, model),
        "project_id": current.get("project_id"),
        "runtime_context_epoch": int(current["runtime_context_epoch"]),
        "content": _owner_map_content(current, rows),
        "agent_count": len(rows),
        "agent_thread_ids": [row["id"] for row in rows],
    }


def current_responsibility_map_snapshot(
    thread_id: str, db, *, model: str | None = None,
) -> dict[str, Any]:
    """Render the exact current responsibility map for one destination."""
    current, rows = agent_rows(thread_id, db)
    selected_model = model if model is not None else str(current.get("model") or "")
    return _map_snapshot(current, rows, model=selected_model)


def _full_roster_content(current: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [_self_line(current, rows)]
    if not rows:
        return "\n".join([
            *lines, "No active Agents are registered in this scope.",
            *_conductor_gap_line(current),
        ])
    has_workspace_contact = current.get("project_id") is not None and any(
        row["project_id"] is None for row in rows
    )
    lines.extend((
        "",
        "Active agents and the CORSIA agent:"
        if has_workspace_contact else "Active agents:",
    ))
    for row in rows:
        scope = row.get("project_id") or "workspace"
        territory = ", ".join(
            [*_json_list(row.get("agent_paths")), *_json_list(row.get("agent_domains"))]
        ) or "unspecified"
        notes = str(row.get("agent_notes") or "").strip() or "none"
        lines.append(
            f"- {row.get('title') or 'Untitled'} ({row['id']}); scope={scope}; "
            f"responsibility={row['agent_responsibility']}; "
            f"paths/domains={territory}; notes={notes}"
        )
    lines.extend(_conductor_gap_line(current))
    return "\n".join(lines)


def _unavailable_map(thread_id: str, reason: str) -> ContextSourceEntry:
    return ContextSourceEntry(
        source_id="role:agent-owner-map", origin_section="role",
        label="Agent Responsibility Map", stable_reference=f"thread://{thread_id}/agents",
        freshness="unavailable", inclusion="omitted",
        reason=f"Agent responsibility map unavailable: {reason}",
        owner_surface="agents",
        metadata={"provider": "agent_owner_map", "agent_count": 0,
                  "repair_owner": "agents"},
    )


def installed_owner_map(
    *, thread_id: str, db, stored: dict[str, Any] | None,
    selected_model: str | None = None,
) -> tuple[ContextSourceEntry, dict[str, Any] | None, str | None]:
    """Return the map row, the map this execution delivers, and why.

    ``stored`` is the install boundary's snapshot, read once by the collector;
    the map is the retained one when that snapshot fits this execution.
    """
    if db is None:
        return _unavailable_map(thread_id, "database unavailable"), None, None
    try:
        current, rows = agent_rows(thread_id, db)
    except sqlite3.Error as exc:
        return _unavailable_map(thread_id, str(exc)), None, None
    model = (
        selected_model if selected_model is not None
        else str(current.get("model") or "")
    )
    snapshot, adoption = installed_map(
        stored, _map_snapshot(current, rows, model=model),
    )
    entry = ContextSourceEntry(
        source_id="role:agent-owner-map", origin_section="role",
        label="Agent Responsibility Map", stable_reference=f"thread://{thread_id}/agents",
        freshness="fresh", inclusion="included",
        reason="stable scoped responsibility map",
        owner_surface="agents", prompt_content=snapshot["content"],
        metadata={"provider": "agent_owner_map",
                  "agent_count": snapshot["agent_count"],
                  "repair_owner": "agents",
                  "agent_thread_ids": snapshot["agent_thread_ids"],
                  RESPONSIBILITY_MAP_SNAPSHOT: snapshot},
    )
    return entry, snapshot, adoption


class AgentRosterTool(Tool):
    """Read full details for active agents visible to the current thread."""

    @property
    def name(self) -> str:
        return "agent_roster"

    @property
    def description(self) -> str:
        return (
            "List full details for active agents in this thread's scope, plus "
            "the CORSIA agent contact for project agents. "
            "Project agents are not visible from workspace scope. "
            "Use agent_roster_entry for one entry and its revision before editing."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        from infra.database import get_db
        from infra.db_async import run_in_db

        thread_id = kwargs.get("_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return "Error: agent_roster requires a current thread"

        def _load() -> str:
            db = get_db()
            try:
                current, rows = agent_rows(thread_id, db)
                return _full_roster_content(current, rows)
            finally:
                db.close()

        return await run_in_db(_load)
