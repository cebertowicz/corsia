"""Shared write helpers for connected-app lifecycle transitions."""

import json
import sqlite3

from connections.account_policy import project_exists
from connections.app_notices import append_app_notice
from connections.app_reach import (
    AppNameOverlapError, AppReach, AppReachError, PENDING_STATE, _reach, key_entry_names,
)
from infra.database import write_transaction
from infra.time import utc_now_iso

def _ensure_app_key_entries(app_id: str, name: str, config: object, *, db: sqlite3.Connection) -> None:
    """Expose approved app keys for a person to set without creating a value."""
    from infra.key_store import ensure_managed_entry

    for entry in key_entry_names(app_id, config):
        ensure_managed_entry(entry, f"{name} key", "Used by this connected app.", db=db)


def _write_audit(
    db: sqlite3.Connection,
    *,
    actor: tuple[str, str] | None,
    app_id: str,
    action: str,
    reach_from: AppReach | None,
    reach_to: AppReach | None,
) -> None:
    if actor is None:
        return
    actor_kind, actor_id = actor
    db.execute(
        "INSERT INTO connected_app_audit("
        "created_at,actor_kind,actor_id,app_id,action,reach_from_json,reach_to_json"
        ") VALUES (?,?,?,?,?,?,?)",
        (
            utc_now_iso(), actor_kind, actor_id, app_id, action,
            json.dumps(reach_from.as_dict(), separators=(",", ":"), sort_keys=True) if reach_from else None,
            json.dumps(reach_to.as_dict(), separators=(",", ":"), sort_keys=True) if reach_to else None,
        ),
    )


def _expire_pending_proposal(conn: sqlite3.Connection, app_id: str, app: sqlite3.Row) -> dict[str, object]:
    """Apply the same durable outcome as a decline once a proposal has expired."""
    current_reach = _reach(app_id, db=conn)
    name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
    if app["pending_config_json"] is None:
        _write_audit(
            conn, actor=("system", "proposal_expiry"), app_id=app_id, action="expire",
            reach_from=current_reach, reach_to=None,
        )
        append_app_notice(
            app_id, f"{name}: removed", db=conn, state="disconnected", reach_before=current_reach,
            removed=True,
        )
        conn.execute("DELETE FROM connected_apps WHERE id=?", (app_id,))
        return {"id": app_id, "removed": True}
    previous = app["pending_previous_state"] or "disconnected"
    now = utc_now_iso()
    conn.execute(
        "UPDATE connected_apps SET state=?,state_since=?,revision=?,pending_config_json=NULL,"
        "pending_previous_state=NULL,pending_actor_thread_id=NULL,updated_at=? "
        "WHERE id=? AND state=?",
        (previous, now, app["revision"] + 1, now, app_id, PENDING_STATE),
    )
    _write_audit(
        conn, actor=("system", "proposal_expiry"), app_id=app_id, action="expire",
        reach_from=current_reach, reach_to=current_reach,
    )
    append_app_notice(
        app_id, f"{name}: {previous}", db=conn, state=previous, reach_after=current_reach,
    )
    return {"id": app_id, "removed": False}


def record_refusal(app_id: str, action: str, *, actor: tuple[str, str], db: sqlite3.Connection | None = None) -> None:
    """Durably retain a refused privileged action without config or key data."""
    with write_transaction(db, scope="connected_app_audit_refusal") as conn:
        _write_audit(conn, actor=actor, app_id=app_id, action=action, reach_from=None, reach_to=None)


def record_sign_in_event(
    app_id: str, action: str, *, actor: tuple[str, str], db: sqlite3.Connection | None = None,
) -> None:
    """Durably retain one value-free event of a hosted app's sign-in: its actor, the app and a fixed action text."""
    with write_transaction(db, scope="connected_app_audit_sign_in") as conn:
        _write_audit(conn, actor=actor, app_id=app_id, action=action, reach_from=None, reach_to=None)


def _config_json(config: object) -> str:
    try:
        return json.dumps(config, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        raise AppReachError("invalid app config") from None


def _write_projects(app_id: str, reach: AppReach, *, db: sqlite3.Connection) -> None:
    if reach.mode == "selected":
        db.executemany(
            "INSERT INTO connected_app_reach(app_id,project_id) VALUES (?,?)",
            ((app_id, project_id) for project_id in sorted(reach.project_ids)),
        )


def _assert_selected_projects_exist(reach: AppReach, *, db: sqlite3.Connection) -> None:
    if reach.mode != "selected":
        return
    missing = sorted(project_id for project_id in reach.project_ids if not project_exists(project_id, db=db))
    if missing:
        raise AppReachError("connected app project missing")


def _assert_name_available(
    name: str, reach: AppReach, *, app_id: str | None = None, db: sqlite3.Connection
) -> None:
    """Refuse only same-name apps whose live project or Outside scopes overlap."""
    rows = db.execute(
        "SELECT id,reach_mode,outside_projects FROM connected_apps WHERE name=? "
        "AND (? IS NULL OR id<>?) ORDER BY id",
        (name, app_id, app_id),
    ).fetchall()
    for row in rows:
        other = _reach(row["id"], db=db)
        conflict = _overlap_scope(reach, other, db=db)
        if conflict is not None:
            raise AppNameOverlapError(conflict)


def _overlap_scope(left: AppReach, right: AppReach, *, db: sqlite3.Connection) -> str | None:
    """Return one overlapping live scope, preferring Outside projects deterministically."""
    left_all = left.mode == "all"
    right_all = right.mode == "all"
    if (left_all or left.outside_projects) and (right_all or right.outside_projects):
        return "outside_projects"
    if left_all and right_all:
        row = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        return row["id"] if row else None
    if left_all:
        candidates = right.project_ids
    elif right_all:
        candidates = left.project_ids
    else:
        candidates = left.project_ids & right.project_ids
    for project_id in sorted(candidates):
        if project_exists(project_id, db=db):
            return project_id
    return None
