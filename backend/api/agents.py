"""Agent roster API."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core.agent_roster_mutations import RosterChangeError, change_agent
from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import thread_available_sql

router = APIRouter(prefix="/api", tags=["agents"])



def _agent_row(row) -> dict[str, Any]:
    paths = json.loads(row["agent_paths"] or "[]")
    domains = json.loads(row["agent_domains"] or "[]")
    return {
        "thread_id": row["id"],
        "display_name": row["title"],
        "project_id": row["project_id"],
        "responsibility": row["agent_responsibility"],
        "responsibility_paths": paths,
        "responsibility_domains": domains,
        "coordination_notes": row["agent_notes"],
        "active": (row["agent_status"] or "active") == "active",
    }


@router.get("/agents")
async def list_agents(project_id: str | None = None, user: dict = Depends(require_user)):
    """List active Agent threads for the requested scope."""
    def _list():
        db = get_db()
        try:
            sql = f"""
                SELECT id, title, project_id, agent_responsibility,
                       agent_paths, agent_domains, agent_status, agent_notes
                FROM threads
                WHERE user_id = ?
                  AND COALESCE(agent_enabled, 0) = 1
                  AND {thread_available_sql()}
                  AND COALESCE(agent_status, 'active') = 'active'
                  AND TRIM(COALESCE(agent_responsibility, '')) != ''
            """
            params: list[Any] = [user["id"]]
            if project_id:
                sql += " AND project_id = ?"
                params.append(project_id)
            else:
                sql += " AND project_id IS NULL"
            sql += " ORDER BY updated_at DESC, id ASC"
            return [_agent_row(row) for row in db.execute(sql, params).fetchall()]
        finally:
            db.close()

    return {"agents": await run_in_db(_list)}


async def _apply_agent_change(thread_id: str, partial: dict, user: dict) -> dict:
    try:
        await run_in_db(change_agent, thread_id, partial, user_id=user["id"])
        return {"ok": True}
    except RosterChangeError as exc:
        status = 404 if str(exc) == "Thread not found" else 400
        raise HTTPException(status, str(exc)) from exc


@router.patch("/agents/{thread_id}")
async def update_agent(thread_id: str, body: dict, user: dict = Depends(require_user)):
    """Promote or replace an Agent; demotion ignores all fields except enabled."""
    enabled = bool(body.get("agent_enabled", True))
    if not enabled:
        # A demote is a partial edit: change_agent's existing partial-update
        # contract keeps the stored responsibility, paths, domains and
        # notes, matching the conductor tool's demote (#2715).
        return await _apply_agent_change(thread_id, {"agent_enabled": False}, user)

    # Promoting or editing an active agent has a full replacement contract.
    responsibility = str(body.get("responsibility") or "").strip()
    if not responsibility:
        raise HTTPException(400, "responsibility required")
    normalized = {
        "agent_enabled": True,
        "active": bool(body.get("active", True)),
        "responsibility": responsibility,
        "responsibility_paths": (
            body["responsibility_paths"] if "responsibility_paths" in body
            else body.get("paths")
        ),
        "responsibility_domains": (
            body["responsibility_domains"] if "responsibility_domains" in body
            else body.get("domains")
        ),
        "coordination_notes": (
            body["coordination_notes"] if "coordination_notes" in body
            else body.get("notes")
        ),
    }
    return await _apply_agent_change(thread_id, normalized, user)


@router.delete("/agents/{thread_id}")
async def demote_agent(thread_id: str, user: dict = Depends(require_user)):
    """Remove Agent status without deleting the thread."""
    return await update_agent(thread_id, {"agent_enabled": False}, user)
