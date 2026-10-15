"""Fetch visible project agents and the CORSIA agent contact."""

from __future__ import annotations

import sqlite3
from typing import Any

from infra.conductor import WORKSPACE_SCOPE, get_conductor
from infra.corsia_agent import CorsiaAgentSettingError
from infra.delegation_scope import native_endpoint_gap
from infra.queries import thread_available_sql

AGENT_COLUMNS = {
    "user_id",
    "agent_enabled",
    "agent_responsibility",
    "agent_paths",
    "agent_domains",
    "agent_status",
    "agent_notes",
}


def _table_columns(db, table: str) -> set[str]:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


_CONTACT_COLUMNS = (
    "id", "title", "project_id", "agent_responsibility", "agent_paths",
    "agent_domains", "agent_status", "agent_notes", "updated_at",
)


def corsia_agent_contact(
    user_id: str, db,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the CORSIA agent contact, or why there is none.

    The contact qualifies under the same endpoint rule native delegate_send
    enforces, so the roster lists the conductor exactly when a send can reach it.
    """
    try:
        conductor_id = get_conductor(WORKSPACE_SCOPE, db=db)
    except CorsiaAgentSettingError:
        return None, "the CORSIA agent setting is malformed"
    if not conductor_id:
        return None, "none designated"
    row = db.execute(
        f"SELECT {','.join(_CONTACT_COLUMNS)},user_id,agent_enabled,"
        "lifecycle_state,archived_reason FROM threads WHERE id=?",
        (conductor_id,),
    ).fetchone()
    if row is None:
        return None, f"designated conductor {conductor_id} does not exist"
    if row["user_id"] != user_id:
        return None, "designated conductor belongs to another account"
    gap = native_endpoint_gap(dict(row), corsia_agent_id=conductor_id)
    if gap:
        return None, f"designated conductor {conductor_id} is unreachable: {gap}"
    contact = {column: row[column] for column in _CONTACT_COLUMNS}
    contact["agent_responsibility"] = (
        contact["agent_responsibility"] or "Designated CORSIA agent."
    )
    return contact, None


def agent_rows(thread_id: str, db) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the caller and only its visible active agents plus workspace contact.

    For a project caller whose CORSIA agent cannot be listed, the
    caller dict carries ``corsia_agent_gap`` so renderers say why.
    """
    columns = _table_columns(db, "threads")
    if not AGENT_COLUMNS <= columns:
        missing = ", ".join(sorted(AGENT_COLUMNS - columns))
        raise sqlite3.OperationalError(f"agent schema unavailable: {missing}")
    current = db.execute(
        "SELECT id,user_id,title,project_id,model,spawned_by,agent_enabled,"
        "agent_responsibility,agent_status,execution_epoch,pending_model,"
        "pending_execution_epoch,runtime_context_epoch FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if current is None:
        raise sqlite3.OperationalError(f"thread does not exist: {thread_id}")
    project_id = current["project_id"]
    scope_sql = "project_id IS NULL" if project_id is None else "project_id=?"
    params: list[Any] = [current["user_id"]]
    if project_id is not None:
        params.append(project_id)
    params.append(thread_id)
    rows = db.execute(
        f"""
        SELECT id,title,project_id,agent_responsibility,agent_paths,
               agent_domains,agent_status,agent_notes,updated_at
        FROM threads
        WHERE user_id=? AND COALESCE(agent_enabled,0)=1
          AND {thread_available_sql()}
          AND COALESCE(agent_status,'active')='active'
          AND TRIM(COALESCE(agent_responsibility,''))!=''
          AND {scope_sql}
        ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,id ASC
        """,
        params,
    ).fetchall()
    visible = [dict(row) for row in rows]
    caller = dict(current)
    if project_id is not None:
        contact, gap = corsia_agent_contact(current["user_id"], db)
        if contact is not None:
            visible.append(contact)
        else:
            caller["corsia_agent_gap"] = gap
    return caller, visible
