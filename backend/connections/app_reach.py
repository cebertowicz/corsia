"""One durable owner for connected-app identity and server-resolved reach."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from connections.account_policy import (
    AccountAdmissionError,
    missing_project_reason,
    project_exists,
    thread_scope,
)
from infra.database import write_transaction
from infra.time import utc_now, utc_now_iso

ReachMode = Literal["all", "selected"]
REACH_MODES = frozenset(("all", "selected"))
PENDING_STATE = "awaiting_approval"
PROPOSAL_EXPIRY = timedelta(days=7)


class AppReachError(ValueError):
    """A stable, value-free connected-app reach error."""


class AppNotFoundError(AppReachError):
    pass


class AppRevisionConflictError(AppReachError):
    pass


class AppPendingApprovalError(AppReachError):
    pass


class AppProposalExpiredError(AppReachError):
    pass


class AppNameOverlapError(AppReachError):
    def __init__(self, scope: str):
        self.scope = scope
        super().__init__(scope)


@dataclass(frozen=True)
class AppReach:
    mode: ReachMode
    project_ids: frozenset[str]
    outside_projects: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "project_ids": sorted(self.project_ids),
            "outside_projects": self.outside_projects,
        }


def normalize_reach(value: object) -> AppReach:
    """Validate the explicit reach shape; an empty Select remains explicit."""
    if not isinstance(value, dict) or set(value) != {"mode", "project_ids", "outside_projects"}:
        raise AppReachError("invalid app reach")
    mode = value["mode"]
    project_ids = value["project_ids"]
    outside_projects = value["outside_projects"]
    if mode not in REACH_MODES or not isinstance(project_ids, list) or not isinstance(outside_projects, bool):
        raise AppReachError("invalid app reach")
    if any(not isinstance(project_id, str) or not project_id for project_id in project_ids):
        raise AppReachError("invalid app reach")
    if len(set(project_ids)) != len(project_ids):
        raise AppReachError("invalid app reach")
    if mode == "all" and (project_ids or outside_projects):
        raise AppReachError("all app reach cannot carry selections")
    return AppReach(mode=mode, project_ids=frozenset(project_ids), outside_projects=outside_projects)


def _reach(app_id: str, *, db: sqlite3.Connection) -> AppReach:
    row = db.execute(
        "SELECT reach_mode, outside_projects FROM connected_apps WHERE id=?", (app_id,)
    ).fetchone()
    if row is None:
        raise AppNotFoundError("connected app not found")
    project_ids = frozenset(
        item["project_id"]
        for item in db.execute(
            "SELECT project_id FROM connected_app_reach WHERE app_id=?", (app_id,)
        )
    )
    return AppReach(
        mode=row["reach_mode"],
        project_ids=project_ids,
        outside_projects=bool(row["outside_projects"]),
    )


def get_reach(app_id: str, *, db: sqlite3.Connection) -> dict[str, object]:
    """Return one app's explicit persisted reach; ids only, never a name lookup."""
    return _reach(app_id, db=db).as_dict()


def key_entry_names(app_id: str, config: object) -> set[str]:
    """Return the managed key-store entries for an app config without reading values."""
    if not isinstance(config, dict):
        raise AppReachError("invalid app config")
    from tools.mcp_secrets import entry_name

    refs = config.get("key_refs")
    if refs is not None:
        if not isinstance(refs, list):
            raise AppReachError("invalid app key references")
        entries: set[str] = set()
        for ref in refs:
            if (
                not isinstance(ref, dict)
                or set(ref) != {"kind", "name"}
                or ref.get("kind") not in {"env", "header"}
                or not isinstance(ref.get("name"), str)
                or not ref["name"]
            ):
                raise AppReachError("invalid app key references")
            field = "secret_env" if ref["kind"] == "env" else "secret_headers"
            entries.add(entry_name(app_id, field, ref["name"]))
        if len(entries) != len(refs):
            raise AppReachError("invalid app key references")
        return entries

    # Migration-created records retain their original secret_* shape, but their
    # entries were rekeyed to the stable app id in the same migration.
    entries = set()
    for field in ("secret_env", "secret_headers"):
        names = config.get(field, [])
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise AppReachError("invalid app key references")
        entries.update(entry_name(app_id, field, name) for name in names)
    return entries


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


def _requesting_agent_name(thread_id: str | None, *, db: sqlite3.Connection) -> str | None:
    """Resolve an approval-card name without making a removed thread authoritative."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
    name = row["title"] if row is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _proposal_expired(state: str, state_since: str) -> bool:
    """Whether this pending proposal has exceeded its seven-day review window."""
    if state != PENDING_STATE:
        return False
    try:
        proposed_at = datetime.fromisoformat(state_since)
    except (TypeError, ValueError):
        return True
    if proposed_at.tzinfo is not None:
        proposed_at = proposed_at.replace(tzinfo=None)
    return utc_now() > proposed_at + PROPOSAL_EXPIRY


def _expire_pending_proposal(conn: sqlite3.Connection, app_id: str, app: sqlite3.Row) -> dict[str, object]:
    """Apply the same durable outcome as a decline once a proposal has expired."""
    current_reach = _reach(app_id, db=conn)
    if app["pending_config_json"] is None:
        _write_audit(
            conn, actor=("system", "proposal_expiry"), app_id=app_id, action="expire",
            reach_from=current_reach, reach_to=None,
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
    return {"id": app_id, "removed": False}


def record_refusal(app_id: str, action: str, *, actor: tuple[str, str], db: sqlite3.Connection | None = None) -> None:
    """Durably retain a refused privileged action without config or key data."""
    with write_transaction(db, scope="connected_app_audit_refusal") as conn:
        _write_audit(conn, actor=actor, app_id=app_id, action=action, reach_from=None, reach_to=None)


def get_app(app_id: str, *, db: sqlite3.Connection) -> dict[str, object]:
    """Read one stable app record without interpreting its opaque config."""
    row = db.execute(
        "SELECT id,name,config_json,state,state_since,revision,created_at,created_by_thread_id,"
        "pending_config_json,pending_previous_state,pending_actor_thread_id "
        "FROM connected_apps WHERE id=?",
        (app_id,),
    ).fetchone()
    if row is None:
        raise AppNotFoundError("connected app not found")
    expired = _proposal_expired(row["state"], row["state_since"])
    return {
        "id": row["id"],
        "name": row["name"],
        "config": json.loads(row["config_json"]),
        "state": "expired" if expired else row["state"],
        "state_since": row["state_since"],
        "revision": row["revision"],
        "reach": _reach(app_id, db=db).as_dict(),
        "created_at": row["created_at"],
        "created_by_thread_id": row["created_by_thread_id"],
        "pending_config": (
            json.loads(row["pending_config_json"])
            if row["pending_config_json"] is not None else None
        ),
        "pending_previous_state": row["pending_previous_state"],
        "pending_actor_thread_id": row["pending_actor_thread_id"],
        "pending_actor_name": _requesting_agent_name(row["pending_actor_thread_id"], db=db),
    }


def list_apps(*, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Read every app by id; callers never resolve an app from an old name."""
    ids = [row["id"] for row in db.execute("SELECT id FROM connected_apps ORDER BY id")]
    return [get_app(app_id, db=db) for app_id in ids]


def app_reaches_thread(app_id: str, thread_id: str, *, db: sqlite3.Connection) -> bool:
    """Whether an app reaches a durable thread scope, resolving no caller input.

    Missing app/thread/project records fail closed. A workspace thread is Outside
    projects, not an implicit project fallback. Every parent must also reach the
    app, so a child can never gain reach its parent lacks.
    """
    try:
        reach = _reach(app_id, db=db)
    except AppReachError:
        return False
    seen: set[str] = set()
    current_thread_id: str | None = thread_id
    while current_thread_id is not None and current_thread_id not in seen:
        seen.add(current_thread_id)
        try:
            scope = thread_scope(current_thread_id, db=db)
        except AccountAdmissionError:
            return False
        if not _reach_scope(reach, scope, db=db):
            return False
        parent = db.execute(
            "SELECT parent_thread_id FROM threads WHERE id=?", (current_thread_id,)
        ).fetchone()
        current_thread_id = parent["parent_thread_id"] if parent is not None else None
    return current_thread_id is None


def _reach_scope(reach: AppReach, scope: dict, *, db: sqlite3.Connection) -> bool:
    if missing_project_reason(scope, db=db):
        return False
    project_id = scope["project_id"]
    if project_id is None:
        return reach.mode == "all" or (
            reach.mode == "selected" and reach.outside_projects
        )
    return reach.mode == "all" or (
        reach.mode == "selected" and project_id in reach.project_ids
    )


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
        _write_audit(
            conn, actor=audit_actor, app_id=identity, action="add",
            reach_from=None, reach_to=normalized,
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
    return {"id": app_id, "revision": next_revision, "reach": normalized.as_dict()}


def create_pending_app(
    name: str,
    config: object,
    reach: object,
    *,
    actor_thread_id: str,
    audit_actor: tuple[str, str] | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Persist an agent's new app proposal without starting it."""
    if not isinstance(actor_thread_id, str) or not actor_thread_id:
        raise AppReachError("missing proposing thread")
    return create_app(
        name,
        config,
        reach,
        state=PENDING_STATE,
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
        conn.execute("DELETE FROM connected_apps WHERE id=?", (app_id,))
    return {"id": app_id, "removed": True}


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
