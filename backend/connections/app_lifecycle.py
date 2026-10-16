"""Connected-app lifecycle writes and proposal transitions."""

import json
import sqlite3
import uuid

from connections.app_lifecycle_support import (
    _assert_name_available, _assert_selected_projects_exist, _config_json,
    _ensure_app_key_entries, _expire_pending_proposal, _write_audit, _write_projects,
)
from connections.app_notices import append_app_notice
from connections.app_reach import (
    AppNotFoundError, AppPendingApprovalError, AppProposalExpiredError,
    AppReachError, AppRevisionConflictError, PENDING_STATE, _proposal_expired,
    _reach, _requesting_agent_name, get_app, key_entry_names, normalize_reach,
)
from infra.database import write_transaction
from infra.time import utc_now_iso

def create_app(
    name: str,
    config: object,
    reach: object,
    *,
    state: str = "disconnected",
    app_id: str | None = None,
    created_by_thread_id: str | None = None,
    pending_actor_thread_id: str | None = None,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Create an app with opaque JSON config and explicit reach in one write."""
    if not isinstance(name, str) or not name.strip():
        raise AppReachError("invalid app name")
    normalized = normalize_reach(reach)
    try:
        config_json = json.dumps(config, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        raise AppReachError("invalid app config") from None
    identity = app_id or str(uuid.uuid4())
    if not isinstance(identity, str) or not identity:
        raise AppReachError("invalid app id")
    now = utc_now_iso()
    with write_transaction(db, scope="connected_app_create") as conn:
        _assert_selected_projects_exist(normalized, db=conn)
        _assert_name_available(name, normalized, db=conn)
        conn.execute(
            "INSERT INTO connected_apps("
            "id,name,config_json,state,state_since,reach_mode,outside_projects,created_by_thread_id,"
            "pending_actor_thread_id,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identity, name, config_json, state, now, normalized.mode,
             int(normalized.outside_projects), created_by_thread_id, pending_actor_thread_id, now, now),
        )
        _write_projects(identity, normalized, db=conn)
        if state != PENDING_STATE:
            _ensure_app_key_entries(identity, name, config, db=conn)
        _write_audit(
            conn, actor=audit_actor, app_id=identity, action="add",
            reach_from=None, reach_to=normalized,
        )
        append_app_notice(
            identity, f"{name}: {state}", db=conn, state=state, reach_after=normalized,
        )
    return {"id": identity, "revision": 1, "reach": normalized.as_dict()}


def replace_reach(
    app_id: str,
    reach: object,
    *,
    expected_revision: int,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Atomically replace one app's reach after an exact optimistic-lock check."""
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise AppReachError("invalid app revision")
    normalized = normalize_reach(reach)
    with write_transaction(db, scope="connected_app_reach_replace") as conn:
        app = conn.execute(
            "SELECT name,state,revision FROM connected_apps WHERE id=?", (app_id,)
        ).fetchone()
        if app is None:
            raise AppNotFoundError("connected app not found")
        if app["state"] == PENDING_STATE:
            raise AppPendingApprovalError("approve or decline the change first")
        if app["revision"] != expected_revision:
            raise AppRevisionConflictError("connected app changed")
        previous_reach = _reach(app_id, db=conn)
        _assert_selected_projects_exist(normalized, db=conn)
        _assert_name_available(app["name"], normalized, app_id=app_id, db=conn)
        conn.execute("DELETE FROM connected_app_reach WHERE app_id=?", (app_id,))
        _write_projects(app_id, normalized, db=conn)
        next_revision = expected_revision + 1
        conn.execute(
            "UPDATE connected_apps SET reach_mode=?, outside_projects=?, revision=?, updated_at=? "
            "WHERE id=? AND revision=?",
            (normalized.mode, int(normalized.outside_projects), next_revision,
             utc_now_iso(), app_id, expected_revision),
        )
        _write_audit(
            conn, actor=audit_actor, app_id=app_id, action="reach_change",
            reach_from=previous_reach, reach_to=normalized,
        )
        append_app_notice(
            app_id, f"{app['name']}: availability changed", db=conn, state=app["state"],
            reach_before=previous_reach, reach_after=normalized,
        )
    return {"id": app_id, "revision": next_revision, "reach": normalized.as_dict()}


def create_pending_app(
    name: str,
    config: object,
    reach: object,
    *,
    actor_thread_id: str,
    app_id: str | None = None,
    asked_by_thread_id: str | None = None,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Persist an agent's new app proposal without starting it."""
    if not isinstance(actor_thread_id, str) or not actor_thread_id:
        raise AppReachError("missing proposing thread")
    if asked_by_thread_id is not None:
        if not isinstance(asked_by_thread_id, str) or not asked_by_thread_id or not isinstance(config, dict):
            raise AppReachError("invalid asking thread")
        config = {**config, "asked_by_thread_id": asked_by_thread_id}
    return create_app(
        name,
        config,
        reach,
        state=PENDING_STATE,
        app_id=app_id,
        created_by_thread_id=actor_thread_id,
        pending_actor_thread_id=actor_thread_id,
        audit_actor=audit_actor,
        db=db,
    )


def propose_config(
    app_id: str,
    config: object,
    *,
    actor_thread_id: str,
    expected_revision: int,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Save an agent edit beside the live config until a person decides it."""
    if not isinstance(actor_thread_id, str) or not actor_thread_id:
        raise AppReachError("missing proposing thread")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise AppReachError("invalid app revision")
    config_json = _config_json(config)
    with write_transaction(db, scope="connected_app_config_propose") as conn:
        app = conn.execute(
            "SELECT state,revision FROM connected_apps WHERE id=?", (app_id,)
        ).fetchone()
        if app is None:
            raise AppNotFoundError("connected app not found")
        if app["state"] == PENDING_STATE:
            raise AppPendingApprovalError("approve or decline the change first")
        if app["revision"] != expected_revision:
            raise AppRevisionConflictError("connected app changed")
        current_reach = _reach(app_id, db=conn)
        next_revision = expected_revision + 1
        conn.execute(
            "UPDATE connected_apps SET state=?,state_since=?,revision=?,pending_config_json=?,"
            "pending_previous_state=?,pending_actor_thread_id=?,updated_at=? "
            "WHERE id=? AND revision=?",
            (PENDING_STATE, utc_now_iso(), next_revision, config_json, app["state"],
             actor_thread_id, utc_now_iso(), app_id, expected_revision),
        )
        _write_audit(
            conn, actor=audit_actor, app_id=app_id, action="edit",
            reach_from=current_reach, reach_to=current_reach,
        )
        name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
        append_app_notice(
            app_id, f"{name}: {PENDING_STATE}", db=conn, state=PENDING_STATE, reach_after=current_reach,
        )
    return get_app(app_id, db=db)


def replace_config(
    app_id: str,
    config: object,
    *,
    expected_revision: int,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Replace approved stored config only; runtime activation stays elsewhere."""
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise AppReachError("invalid app revision")
    config_json = _config_json(config)
    with write_transaction(db, scope="connected_app_config_replace") as conn:
        app = conn.execute(
            "SELECT state,revision FROM connected_apps WHERE id=?", (app_id,)
        ).fetchone()
        if app is None:
            raise AppNotFoundError("connected app not found")
        if app["state"] == PENDING_STATE:
            raise AppPendingApprovalError("approve or decline the change first")
        if app["revision"] != expected_revision:
            raise AppRevisionConflictError("connected app changed")
        current_reach = _reach(app_id, db=conn)
        next_revision = expected_revision + 1
        now = utc_now_iso()
        conn.execute(
            "UPDATE connected_apps SET config_json=?,state='disconnected',state_since=?,revision=?,updated_at=? "
            "WHERE id=? AND revision=?",
            (config_json, now, next_revision, now, app_id, expected_revision),
        )
        _write_audit(
            conn, actor=audit_actor, app_id=app_id, action="edit",
            reach_from=current_reach, reach_to=current_reach,
        )
        name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
        _ensure_app_key_entries(app_id, name, config, db=conn)
        append_app_notice(
            app_id, f"{name}: disconnected", db=conn, state="disconnected", reach_after=current_reach,
        )
    return get_app(app_id, db=db)


def approve_proposal(
    app_id: str, *, audit_actor: tuple[str, str] | None = None, db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Promote a pending config, unless its seven-day review window has elapsed."""
    expired = False
    expired_agent_name: str | None = None
    with write_transaction(db, scope="connected_app_proposal_approve") as conn:
        app = conn.execute(
            "SELECT revision,pending_config_json,state_since,pending_previous_state,pending_actor_thread_id "
            "FROM connected_apps WHERE id=? AND state=?",
            (app_id, PENDING_STATE),
        ).fetchone()
        if app is None:
            raise AppPendingApprovalError("no connected app change is awaiting approval")
        if _proposal_expired(PENDING_STATE, app["state_since"]):
            expired_agent_name = _requesting_agent_name(app["pending_actor_thread_id"], db=conn)
            _expire_pending_proposal(conn, app_id, app)
            expired = True
        else:
            current_reach = _reach(app_id, db=conn)
            now = utc_now_iso()
            next_revision = app["revision"] + 1
            config_json = app["pending_config_json"]
            conn.execute(
                "UPDATE connected_apps SET config_json=COALESCE(?,config_json),state='disconnected',"
                "state_since=?,revision=?,pending_config_json=NULL,pending_previous_state=NULL,"
                "pending_actor_thread_id=NULL,updated_at=? WHERE id=? AND state=?",
                (config_json, now, next_revision, now, app_id, PENDING_STATE),
            )
            _write_audit(
                conn, actor=audit_actor, app_id=app_id, action="approve",
                reach_from=current_reach, reach_to=current_reach,
            )
            approved = conn.execute(
                "SELECT name,config_json FROM connected_apps WHERE id=?", (app_id,)
            ).fetchone()
            name = approved["name"]
            _ensure_app_key_entries(app_id, name, json.loads(approved["config_json"]), db=conn)
            append_app_notice(
                app_id, f"{name}: disconnected", db=conn, state="disconnected", reach_after=current_reach,
            )
    if expired:
        agent_name = expired_agent_name or "the agent"
        raise AppProposalExpiredError(
            f"This request expired after seven days. Ask {agent_name} to send it again."
        )
    return get_app(app_id, db=db)


def decline_proposal(
    app_id: str, *, audit_actor: tuple[str, str] | None = None, db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Discard a proposal; expiry applies the same state outcome and system audit."""
    with write_transaction(db, scope="connected_app_proposal_decline") as conn:
        app = conn.execute(
            "SELECT revision,pending_config_json,pending_previous_state,state_since FROM connected_apps "
            "WHERE id=? AND state=?",
            (app_id, PENDING_STATE),
        ).fetchone()
        if app is None:
            raise AppPendingApprovalError("no connected app change is awaiting approval")
        if _proposal_expired(PENDING_STATE, app["state_since"]):
            outcome = _expire_pending_proposal(conn, app_id, app)
            if outcome["removed"]:
                return outcome
        else:
            current_reach = _reach(app_id, db=conn)
            if app["pending_config_json"] is None:
                _write_audit(
                    conn, actor=audit_actor, app_id=app_id, action="decline",
                    reach_from=current_reach, reach_to=None,
                )
                name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
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
                conn, actor=audit_actor, app_id=app_id, action="decline",
                reach_from=current_reach, reach_to=current_reach,
            )
            name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
            append_app_notice(
                app_id, f"{name}: {previous}", db=conn, state=previous, reach_after=current_reach,
            )
    return get_app(app_id, db=db)


def delete_app(
    app_id: str,
    *,
    remove_keys: bool,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Delete an app; people remove its stable-id keys and agents deliberately retain them."""
    with write_transaction(db, scope="connected_app_delete") as conn:
        app = conn.execute(
            "SELECT config_json,pending_config_json FROM connected_apps WHERE id=?", (app_id,)
        ).fetchone()
        if app is None:
            raise AppNotFoundError("connected app not found")
        current_reach = _reach(app_id, db=conn)
        if remove_keys:
            key_names = key_entry_names(app_id, json.loads(app["config_json"]))
            if app["pending_config_json"] is not None:
                key_names |= key_entry_names(app_id, json.loads(app["pending_config_json"]))
            if key_names:
                marks = ",".join("?" * len(key_names))
                conn.execute(f"DELETE FROM key_store_entries WHERE name IN ({marks})", tuple(sorted(key_names)))
                from infra.key_store_values import sweep_after_commit
                sweep_after_commit(conn)
        _write_audit(
            conn, actor=audit_actor, app_id=app_id, action="remove",
            reach_from=current_reach, reach_to=None,
        )
        name = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()["name"]
        append_app_notice(
            app_id, f"{name}: removed", db=conn, state="disconnected", reach_before=current_reach,
            removed=True,
        )
        conn.execute("DELETE FROM connected_apps WHERE id=?", (app_id,))
    return {"id": app_id, "removed": True}
