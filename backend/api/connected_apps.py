"""Connected-app authorization, lifecycle, and runtime API routes."""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from api.connected_app_rows import _agent_next_step, app_reaches_project, server_row
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db

connected_apps_router = APIRouter()
log = logging.getLogger(__name__)
LOCAL_APP_NOTICE = (
    "It can do anything you can do on this Mac, except open CORSIA's own files and saved keys. "
    "Connect only apps you trust."
)


def _refusal(detail: str, status: int = 422) -> HTTPException:
    """Return a connected-app validation error with an explicit safe agent remedy."""
    headers = {"X-Corsia-Agent-Remedy": detail} if status == 403 else None
    return HTTPException(status, detail, headers=headers)


def _audit_actor(user: dict) -> tuple[str, str]:
    """Return a durable, value-free actor identity for connected-app audit rows."""
    if is_person_session(user):
        actor = user.get("id")
        return "person", actor if isinstance(actor, str) and actor else "unknown"
    actor = user.get("thread_id") or user.get("id")
    return "agent", actor if isinstance(actor, str) and actor else "unknown"


def _app_error(exc: Exception) -> HTTPException:
    from connections.app_reach import (
        AppNotFoundError,
        AppPendingApprovalError,
        AppProposalExpiredError,
        AppRevisionConflictError,
    )

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, AppNotFoundError):
        return _refusal("connected app not found", 404)
    if isinstance(exc, AppRevisionConflictError):
        return _refusal("connected app changed; refresh it and retry", 409)
    if isinstance(exc, AppProposalExpiredError):
        return _refusal(str(exc), 409)
    if isinstance(exc, AppPendingApprovalError):
        return _refusal("approve or decline the change first", 409)
    return _refusal(str(exc))


async def _run_app_runtime(action: Literal["connect", "reconnect", "remove"], app_id: str) -> dict[str, object]:
    """Send a persisted Connected Apps action to its app-id runtime seam."""
    from tools.connected_apps_runtime import apply_connected_app_runtime
    return await apply_connected_app_runtime(action, app_id)


def _agent_thread(user: dict, db) -> tuple[str, dict]:
    """Resolve an agent's scope from its server-provided current thread only."""
    thread_id = user.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise _refusal(
            "Connected-app changes from an agent need its current thread context; call this through corsia_api from that thread.",
            403,
        )
    from connections.account_policy import AccountAdmissionError, thread_scope
    try:
        scope = thread_scope(thread_id, db=db)
    except AccountAdmissionError:
        raise _refusal("The agent thread no longer exists; start from its current thread and try again.", 403) from None
    if scope["user_id"] != user.get("id"):
        raise _refusal("The agent thread does not belong to this caller.", 403)
    return thread_id, scope


def _agent_reach(thread_id: str, scope: dict, *, db) -> dict[str, object]:
    """Limit ordinary agents to their place; CORSIA proposes workspace apps for all projects."""
    from infra.corsia_agent import is_corsia_agent

    if is_corsia_agent(thread_id, db=db):
        return {"mode": "all", "project_ids": [], "outside_projects": False}
    project_id = scope["project_id"]
    return {
        "mode": "selected",
        "project_ids": [project_id] if project_id is not None else [],
        "outside_projects": project_id is None,
    }


def _expected_revision(body: dict[str, Any]) -> int:
    revision = body.get("expected_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise _refusal("expected_revision must be a positive integer")
    return revision


def _key_refs(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _refusal("key_refs must be a list of env or header names")
    refs: list[dict[str, str]] = []
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {"kind", "name"}
                or ref.get("kind") not in {"env", "header"}
                or not isinstance(ref.get("name"), str) or not ref["name"]):
            raise _refusal("key_refs must contain only env or header names")
        refs.append({"kind": ref["kind"], "name": ref["name"]})
    if len({(ref["kind"], ref["name"]) for ref in refs}) != len(refs):
        raise _refusal("key_refs cannot repeat a name")
    return refs


def _config(body: dict[str, Any]) -> dict[str, Any]:
    config = body.get("config")
    if not isinstance(config, dict) or "key_refs" in config:
        raise _refusal("config must be an object; declare key names with key_refs")
    from tools.conductor_api_policy import contains_forbidden_fields

    refs = _key_refs(body.get("key_refs"))
    if contains_forbidden_fields(config) or contains_forbidden_fields(refs):
        raise _refusal("Credential values are refused. Declare only the key names in key_refs; the person sets values in Settings > Keys.")
    return {**config, "key_refs": refs}


def _local_app_notice(config: object) -> str | None:
    return LOCAL_APP_NOTICE if isinstance(config, dict) and isinstance(config.get("command"), str) else None


# List projections are kept in connected_app_rows to preserve this route module's size budget.

@connected_apps_router.get("/servers")
async def list_servers(
    project: str | None = Query(None),
    place: str | None = Query(None),
    user: dict = Depends(require_user),
) -> dict[str, object]:
    """List connected apps with an agent's inaccessible rows safely projected."""
    human = is_person_session(user)
    if not human and (project is not None or place is not None):
        raise _refusal(
            "Connected-app reach comes from your current thread; remove project or place and retry.",
            403,
        )
    if project is not None and place is not None:
        raise _refusal("choose project or place, not both")
    if place is not None and place != "workspace":
        raise _refusal("place must be workspace")

    def read() -> list[dict[str, object]]:
        from connections.app_reach import app_reaches_thread, list_apps
        from connections.account_policy import project_exists

        db = _db()
        apps = list_apps(db=db)
        if human:
            if project is not None:
                if not project_exists(project, db=db):
                    raise _refusal("project not found", 404)
                return [server_row(app, on_here=app_reaches_project(app, project), full=True, db=db) for app in apps]
            if place == "workspace":
                return [server_row(app, on_here=True, full=True, db=db) for app in apps if app["reach"]["mode"] == "all"]
            return [server_row(app, on_here=True, full=True, db=db) for app in apps]
        thread_id, _scope = _agent_thread(user, db)
        return [
            server_row(app, on_here=reachable, full=reachable, db=db)
            for app in apps
            for reachable in (app_reaches_thread(app["id"], thread_id, db=db),)
        ]

    return {"servers": await run_in_db(read)}


def _db():
    from infra.database import get_db
    return get_db()


@connected_apps_router.post("/servers", status_code=201)
async def create_server(body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """Create an approved human config or an inert agent proposal."""
    name = body.get("name")
    config = _config(body)
    human = is_person_session(user)
    if not human and "reach" in body:
        raise _refusal("Connected-app reach comes from your current thread; remove reach and retry.", 403)
    if human:
        reach = body.get("reach")
        if reach is None:
            raise _refusal("reach is required when a person connects an app")

        def create() -> dict[str, object]:
            from connections.app_lifecycle import create_app
            return create_app(name, config, reach, audit_actor=_audit_actor(user), db=_db())
    else:
        def create() -> dict[str, object]:
            from connections.app_lifecycle import create_pending_app
            thread_id, scope = _agent_thread(user, _db())
            return create_pending_app(
                name, config, _agent_reach(thread_id, scope, db=_db()), actor_thread_id=thread_id,
                audit_actor=_audit_actor(user), db=_db(),
            )
    try:
        result = await run_in_db(create)
    except Exception as exc:
        raise _app_error(exc) from None
    if human:
        await _run_app_runtime("connect", result["id"])
    notice = _local_app_notice(config)
    return {**result, **({"notice": notice} if notice else {})}


@connected_apps_router.patch("/servers/{app_id}")
async def update_server(app_id: str, body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """Save a direct human edit or an agent proposal without touching runtime state."""
    config = _config(body)
    revision = _expected_revision(body)
    human = is_person_session(user)
    if not human and "reach" in body:
        raise _refusal("Agents cannot change where an app works; ask the person in Connected apps.", 403)

    def update() -> dict[str, object]:
        if human:
            from connections.app_lifecycle import replace_config
            return replace_config(
                app_id, config, expected_revision=revision, audit_actor=_audit_actor(user), db=_db()
            )
        from connections.app_lifecycle import propose_config
        from connections.app_reach import app_reaches_thread
        thread_id, _scope = _agent_thread(user, _db())
        if not app_reaches_thread(app_id, thread_id, db=_db()):
            raise _refusal("This connected app is not available to your project; ask the person in Connected apps.", 403)
        return propose_config(
            app_id, config, actor_thread_id=thread_id, expected_revision=revision,
            audit_actor=_audit_actor(user), db=_db(),
        )

    try:
        result = await run_in_db(update)
    except Exception as exc:
        raise _app_error(exc) from None
    if human:
        await _run_app_runtime("reconnect", app_id)
    return result


@connected_apps_router.put("/servers/{app_id}/reach")
async def update_server_reach(app_id: str, body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """A person changes reach through the core's revision-checked transaction."""
    if not is_person_session(user):
        raise _refusal("Only the signed-in person can change where an app works; open Connected apps.", 403)
    if set(body) != {"reach", "expected_revision"}:
        raise _refusal("reach changes need reach and expected_revision")
    try:
        from connections.app_lifecycle import replace_reach
        result = await run_in_db(
            lambda: replace_reach(
                app_id, body["reach"], expected_revision=_expected_revision(body),
                audit_actor=_audit_actor(user), db=_db()
            )
        )
    except Exception as exc:
        raise _app_error(exc) from None
    return result


def _human_decision(user: dict, action: str, app_id: str) -> None:
    if is_person_session(user):
        return
    from connections.app_lifecycle_support import record_refusal

    record_refusal(app_id, f"refused_{action}", actor=_audit_actor(user), db=_db())
    raise _refusal("Only the signed-in person can approve or decline an app; open Connected apps.", 403)


@connected_apps_router.post("/servers/{app_id}/approve")
async def approve_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Approve an inert proposal; connection/reload is deliberately not part of this route."""
    _human_decision(user, "approve", app_id)
    try:
        from connections.app_lifecycle import approve_proposal
        result = await run_in_db(
            lambda: approve_proposal(app_id, audit_actor=_audit_actor(user), db=_db())
        )
    except Exception as exc:
        raise _app_error(exc) from None
    await _run_app_runtime("connect", app_id)
    notice = _local_app_notice(result["config"])
    return {**result, **({"notice": notice} if notice else {})}


@connected_apps_router.post("/servers/{app_id}/decline")
async def decline_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Decline an inert proposal and retain an earlier approved config if there was one."""
    _human_decision(user, "decline", app_id)
    try:
        from connections.app_lifecycle import decline_proposal
        result = await run_in_db(
            lambda: decline_proposal(app_id, audit_actor=_audit_actor(user), db=_db())
        )
    except Exception as exc:
        raise _app_error(exc) from None
    return result


@connected_apps_router.delete("/servers/{app_id}")
async def delete_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Remove config; a person's delete also removes only this app's stable-id keys."""
    human = is_person_session(user)

    def delete() -> dict[str, object]:
        from connections.app_lifecycle import delete_app
        from connections.app_reach import app_reaches_thread

        if not human:
            thread_id, _scope = _agent_thread(user, _db())
            if not app_reaches_thread(app_id, thread_id, db=_db()):
                raise _refusal("This connected app is not available to your project; ask the person in Connected apps.", 403)
        return delete_app(app_id, remove_keys=human, audit_actor=_audit_actor(user), db=_db())

    try:
        result = await run_in_db(delete)
    except Exception as exc:
        raise _app_error(exc) from None
    await _run_app_runtime("remove", app_id)
    return result


@connected_apps_router.post("/servers/{app_id}/reconnect")
async def reconnect_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Reconnect exactly one reachable app, bounded to one restart per ten seconds."""
    def authorize() -> None:
        from connections.app_reach import app_reaches_thread, get_app

        if is_person_session(user):
            get_app(app_id, db=_db())
            return
        thread_id, _scope = _agent_thread(user, _db())
        if not app_reaches_thread(app_id, thread_id, db=_db()):
            raise _refusal(
                "This connected app is not available to your project; ask the person in Connected apps.",
                403,
            )

    try:
        await run_in_db(authorize)
        state = await _run_app_runtime("reconnect", app_id)
    except Exception as exc:
        from tools.mcp_runtime import MCPReconnectRateLimitError

        if isinstance(exc, MCPReconnectRateLimitError):
            raise _refusal(str(exc), 429) from None
        raise _app_error(exc) from None
    return {"id": app_id, **state}
