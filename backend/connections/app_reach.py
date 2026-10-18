"""One durable owner for connected-app identity and server-resolved reach."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from connections.account_policy import AccountAdmissionError, missing_project_reason, thread_scope
from connections.app_source_folder import SOURCE_FOLDER_KEY, clean_folder_label
from infra.time import utc_now

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


def key_ref_descriptors(app_id: str, config: object, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Return declared app keys with set state, never their values or value references."""
    if not isinstance(config, dict) or not isinstance(config.get("key_refs"), list):
        raise AppReachError("invalid app key references")
    from tools.mcp_secrets import entry_name

    refs = config["key_refs"]
    entries = key_entry_names(app_id, config)
    if not refs:
        return []
    marks = ",".join("?" for _ in entries)
    set_entries = {
        row["name"] for row in db.execute(
            f"SELECT name FROM key_store_entries WHERE name IN ({marks}) AND value_ref IS NOT NULL",
            tuple(entries),
        )
    }
    result: list[dict[str, object]] = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"kind", "name"}:
            raise AppReachError("invalid app key references")
        field = "secret_env" if ref["kind"] == "env" else "secret_headers"
        result.append({
            "kind": ref["kind"],
            "name": ref["name"],
            "set": entry_name(app_id, field, ref["name"]) in set_entries,
        })
    return result


def with_entry_names(app_id: str, key_refs: list[dict[str, object]]) -> list[dict[str, object]]:
    """Add each key's managed entry name for the person's Settings; agent projections never call this."""
    from tools.mcp_secrets import entry_name

    return [
        {**ref, "entry": entry_name(app_id, "secret_env" if ref["kind"] == "env" else "secret_headers", str(ref["name"]))}
        for ref in key_refs
    ]


def missing_app_key_refs(app_id: str, *, db: sqlite3.Connection) -> list[str]:
    """Return unset declared key names without resolving their values."""
    app = get_app(app_id, db=db)
    return [
        str(ref["name"])
        for ref in key_ref_descriptors(app_id, app["config"], db=db)
        if not ref["set"]
    ]


def _requesting_agent_name(thread_id: str | None, *, db: sqlite3.Connection) -> str | None:
    """Resolve an approval-card name without making a removed thread authoritative."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    row = db.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
    name = row["title"] if row is not None else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _asked_by(thread_id: str | None, *, db: sqlite3.Connection) -> dict[str, str] | None:
    """Resolve an asserted requester from the registry, never caller-supplied labels."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    row = db.execute(
        "SELECT t.title AS name,p.name AS project FROM threads t LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.id=?", (thread_id,),
    ).fetchone()
    if row is None or not isinstance(row["name"], str) or not row["name"].strip():
        return None
    return {"name": row["name"].strip(), "project": row["project"] if isinstance(row["project"], str) else ""}


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
    stored_config = json.loads(row["config_json"])
    asked_by_thread_id = stored_config.pop("asked_by_thread_id", None) if isinstance(stored_config, dict) else None
    source_folder = clean_folder_label(stored_config.pop(SOURCE_FOLDER_KEY, None)) if isinstance(stored_config, dict) else None
    expired = _proposal_expired(row["state"], row["state_since"])
    return {
        "id": row["id"],
        "name": row["name"],
        "config": stored_config,
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
        **({"asked_by": asker, "asked_by_claim": "agent"}
           if (asker := _asked_by(asked_by_thread_id, db=db)) is not None else {}),
        **({"source_folder": source_folder} if source_folder is not None else {}),
    }


def list_apps(*, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Read every app in a fixed order (name, then id); callers never resolve an app from an old name."""
    ids = [row["id"] for row in db.execute("SELECT id FROM connected_apps ORDER BY name COLLATE NOCASE, name, id")]
    return [get_app(app_id, db=db) for app_id in ids]


def runtime_app_config(app_id: str, *, db: sqlite3.Connection) -> tuple[str, dict[str, object]]:
    """Build one launch config keyed by its stable app id without reading any key value."""
    app = get_app(app_id, db=db)
    if app["state"] in {PENDING_STATE, "expired"}:
        raise AppPendingApprovalError("no approved connected app is available to start")
    config = app["config"]
    if not isinstance(config, dict):
        raise AppReachError("invalid app config")
    runtime_config = dict(config)
    if "key_refs" in runtime_config:
        refs = runtime_config.pop("key_refs")
        if not isinstance(refs, list):
            raise AppReachError("invalid app key references")
        runtime_config["secret_env"] = [
            ref["name"] for ref in refs if isinstance(ref, dict) and ref.get("kind") == "env"
        ]
        runtime_config["secret_headers"] = [
            ref["name"] for ref in refs if isinstance(ref, dict) and ref.get("kind") == "header"
        ]
        if len(runtime_config["secret_env"]) + len(runtime_config["secret_headers"]) != len(refs):
            raise AppReachError("invalid app key references")
    runtime_config["connected_app_id"] = app_id
    name = app["name"]
    if not isinstance(name, str):
        raise AppReachError("invalid app name")
    return name, runtime_config


def runtime_app_ids(*, db: sqlite3.Connection) -> list[str]:
    """Return approved app ids eligible for runtime startup."""
    return [row["id"] for row in db.execute(
        "SELECT id FROM connected_apps WHERE state<>? ORDER BY id", (PENDING_STATE,)
    )]


def load_runtime_app_config(app_id: str) -> tuple[str, dict[str, object]]:
    """Load one launch config in a DB job, retaining no caller-owned connection."""
    from infra.database import get_db

    db = get_db()
    try:
        return runtime_app_config(app_id, db=db)
    finally:
        db.close()


def load_missing_app_key_refs(app_id: str) -> list[str]:
    """Load unset key references in a DB job without retaining a connection."""
    from infra.database import get_db

    db = get_db()
    try:
        return missing_app_key_refs(app_id, db=db)
    finally:
        db.close()


def load_runtime_app_ids() -> list[str]:
    """Load all startup app ids in one DB job."""
    from infra.database import get_db

    db = get_db()
    try:
        return runtime_app_ids(db=db)
    finally:
        db.close()


def runtime_app_id_for_key_entry(entry: str) -> str | None:
    """Find the approved app using one managed key entry, without reading its value."""
    from infra.database import get_db

    db = get_db()
    try:
        for row in db.execute("SELECT id,config_json,state,state_since FROM connected_apps"):
            if row["state"] == PENDING_STATE or _proposal_expired(row["state"], row["state_since"]):
                continue
            if entry in key_entry_names(row["id"], json.loads(row["config_json"])):
                return row["id"]
        return None
    finally:
        db.close()


def _reach_reaches_thread(reach: AppReach, thread_id: str, *, db: sqlite3.Connection) -> bool:
    """Apply one reach snapshot to a thread and every parent, failing closed."""
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


def reach_reaches_thread(reach: AppReach, thread_id: str, *, db: sqlite3.Connection) -> bool:
    """Apply one persisted reach shape to a durable thread, failing closed.

    Connected accounts and hosted apps deliberately share this resolver so a
    scope rule has the same parent-chain semantics for both row kinds.
    """
    return _reach_reaches_thread(reach, thread_id, db=db)


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
    return reach_reaches_thread(reach, thread_id, db=db)


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
