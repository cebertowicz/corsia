"""Connected-app authorization, lifecycle, and runtime API routes."""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request

from api.connected_app_access import audit_actor as _audit_actor, person_only, refusal as _refusal
from api.connected_app_rows import _agent_next_step, app_reaches_project, local_app_notice as _local_app_notice, server_row
from api.connected_app_errors import app_error as _app_error
from api.connected_account_rows import accounts_router, list_account_rows
from api.connected_app_signin import sign_in_here, signin_router
from infra.auth import is_person_session, require_user
from infra.db_async import run_in_db
from tools.mcp_oauth_listener import oauth_settings
from tools.mcp_oauth_signout import delete_with_sign_in, remove_app

connected_apps_router = APIRouter()
connected_apps_router.include_router(signin_router)
connected_apps_router.include_router(accounts_router)
log = logging.getLogger(__name__)
BODY_SHAPE = (
    'Send {"name": "GitHub", "config": {"command": "npx", "args": ["-y", "<package>"]}, '
    '"key_refs": [{"kind": "env", "name": "GITHUB_TOKEN"}]}; use "url" instead of "command" for a hosted app.'
)


async def _run_app_runtime(action: Literal["connect", "restart", "reconnect", "remove"], app_id: str) -> dict[str, object]:
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


def _reaching_thread(user: dict, app_id: str) -> str:
    """The agent's thread, or a refusal when its project can't reach ``app_id``."""
    from connections.app_reach import app_reaches_thread

    thread_id, _scope = _agent_thread(user, _db())
    if not app_reaches_thread(app_id, thread_id, db=_db()):
        raise _refusal("This connected app is not available to your project; ask the person in Connected apps.", 403)
    return thread_id


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
        raise _refusal(f"key_refs must be a list of env or header names. {BODY_SHAPE}")
    refs: list[dict[str, str]] = []
    for ref in value:
        if (not isinstance(ref, dict) or set(ref) != {"kind", "name"}
                or ref.get("kind") not in {"env", "header"}
                or not isinstance(ref.get("name"), str) or not ref["name"]):
            raise _refusal(f"key_refs must contain only env or header names. {BODY_SHAPE}")
        refs.append({"kind": ref["kind"], "name": ref["name"]})
    if len({(ref["kind"], ref["name"]) for ref in refs}) != len(refs):
        raise _refusal("key_refs cannot repeat a name")
    return refs


def _config(body: dict[str, Any]) -> dict[str, Any]:
    config = body.get("config")
    if isinstance(config, dict) and {"key_refs", "asked_by_thread_id"}.intersection(config):
        raise _refusal(f"key_refs and asked_by_thread_id go beside config, not inside it. {BODY_SHAPE}")
    if isinstance(config, dict) and "source_folder" in config:
        raise _refusal(f"source_folder is set by CORSIA when it imports an app, not inside config. {BODY_SHAPE}")
    if not isinstance(config, dict):
        raise _refusal(f"config must be an object. {BODY_SHAPE}")
    command, url, args = config.get("command"), config.get("url"), config.get("args", [])
    if not (isinstance(command, str) and command.strip()) and not (isinstance(url, str) and url.strip()):
        raise _refusal(f"config needs the command that starts the app, or its url. {BODY_SHAPE}")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise _refusal(f"config args must be a list of strings. {BODY_SHAPE}")
    from tools.conductor_api_policy import contains_forbidden_fields

    refs = _key_refs(body.get("key_refs"))
    if contains_forbidden_fields(config) or contains_forbidden_fields(refs):
        raise _refusal("Credential values are refused. Declare only the key names in key_refs; the person sets values in Settings > Keys.")
    try:
        oauth_settings(config)
        from tools.mcp_secrets import header_schemes

        header_schemes({**config, "secret_headers": [ref["name"] for ref in refs if ref["kind"] == "header"]})
    except Exception as error:
        raise _refusal(str(error)) from None
    return {**config, "key_refs": refs}


# List projections are kept in connected_app_rows to preserve this route module's size budget.

@connected_apps_router.get("/servers")
async def list_servers(
    request: Request,
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

    def read_accounts() -> list[dict[str, object]]:
        """Google accounts: a person's view (filtered like apps), or an agent's reachable ones."""
        db = _db()
        thread_id = None if human else _agent_thread(user, db)[0]
        rows = list_account_rows(thread_id, db=db)
        if project is not None:
            return [row for row in rows if app_reaches_project(row, project)]
        return [row for row in rows if row["reach"]["mode"] == "all"] if place == "workspace" else rows

    def read() -> list[dict[str, object]]:
        from connections.app_reach import app_reaches_thread, list_apps
        from connections.account_policy import project_exists

        db = _db()
        apps = list_apps(db=db)
        if human:
            if project is not None:
                if not project_exists(project, db=db):
                    raise _refusal("project not found", 404)
                return [server_row(app, on_here=app_reaches_project(app, project), full=True, db=db, person=True) for app in apps]
            if place == "workspace":
                return [server_row(app, on_here=True, full=True, db=db, person=True) for app in apps if app["reach"]["mode"] == "all"]
            return [server_row(app, on_here=True, full=True, db=db, person=True) for app in apps]
        thread_id, _scope = _agent_thread(user, db)
        return [
            server_row(app, on_here=reachable, full=reachable, db=db)
            for app in apps
            for reachable in (app_reaches_thread(app["id"], thread_id, db=db),)
        ]

    return {
        "servers": await run_in_db(read), "accounts": await run_in_db(read_accounts),
        "sign_in_here": sign_in_here(request),
    }


def _db():
    from infra.database import get_db
    return get_db()


@connected_apps_router.post("/servers", status_code=201)
async def create_server(body: dict[str, Any], user: dict = Depends(require_user)) -> dict[str, object]:
    """Create an approved human config or an inert agent proposal."""
    name = body.get("name")
    config = _config(body)
    human = is_person_session(user)
    asked_by_thread_id = body.get("asked_by_thread_id")
    if asked_by_thread_id is not None and (not isinstance(asked_by_thread_id, str) or not asked_by_thread_id):
        raise _refusal("asked_by_thread_id must be a nonempty thread id")
    if human and asked_by_thread_id is not None:
        raise _refusal("asked_by_thread_id is for a workspace agent proposal only", 403)
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
            from connections.account_policy import AccountAdmissionError, thread_scope
            from connections.app_lifecycle import create_pending_app
            from infra.corsia_agent import is_corsia_agent

            thread_id, scope = _agent_thread(user, _db())
            if asked_by_thread_id is not None:
                if not is_corsia_agent(thread_id, db=_db()):
                    raise _refusal("Only the workspace CORSIA agent can name an asking agent.", 403)
                try:
                    thread_scope(asked_by_thread_id, db=_db())
                except AccountAdmissionError:
                    raise _refusal("The asking agent thread no longer exists; use its current thread id.") from None
            return create_pending_app(
                name, config, _agent_reach(thread_id, scope, db=_db()), actor_thread_id=thread_id,
                asked_by_thread_id=asked_by_thread_id, audit_actor=_audit_actor(user), db=_db(),
            )
    try:
        result = await run_in_db(create)
    except Exception as exc:
        raise _app_error(exc) from None
    runtime = await _run_app_runtime("connect", result["id"]) if human else {}
    notice = _local_app_notice(config)
    return {**result, **runtime, **({"notice": notice} if notice else {})}


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
        thread_id = _reaching_thread(user, app_id)
        return propose_config(
            app_id, config, actor_thread_id=thread_id, expected_revision=revision,
            audit_actor=_audit_actor(user), db=_db(),
        )

    try:
        result = await run_in_db(update)
    except Exception as exc:
        raise _app_error(exc) from None
    if human:
        from tools.connected_apps_runtime import restart_after_saved_write
        await restart_after_saved_write(app_id)
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


_DECIDE_ONLY = "Only the signed-in person can approve or decline an app; open Connected apps."


@connected_apps_router.post("/servers/{app_id}/approve")
async def approve_server(
    app_id: str, body: dict[str, Any] | None = Body(None), user: dict = Depends(require_user),
) -> dict[str, object]:
    """Approve a proposal, optionally choosing where it works in the same revision-checked write."""
    person_only(user, "approve", app_id, copy=_DECIDE_ONLY, db=_db())
    body = body or {}
    if not set(body) <= {"reach", "expected_revision"}:
        raise _refusal("approval takes only reach and expected_revision")
    revision = _expected_revision(body)
    try:
        from connections.app_lifecycle import approve_proposal
        result = await run_in_db(
            lambda: approve_proposal(
                app_id, reach=body.get("reach"), expected_revision=revision,
                audit_actor=_audit_actor(user), db=_db(),
            )
        )
    except Exception as exc:
        raise _app_error(exc) from None
    state = await _run_app_runtime("connect", app_id)
    notice = _local_app_notice(result["config"])
    return {**result, **state, **({"notice": notice} if notice else {})}


@connected_apps_router.post("/servers/{app_id}/decline")
async def decline_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Decline an inert proposal and retain an earlier approved config if there was one."""
    person_only(user, "decline", app_id, copy=_DECIDE_ONLY, db=_db())
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
    """Remove config and its sign-in in one write, then revoke it; a person's delete also removes only this app's keys."""
    if not (human := is_person_session(user)):
        await run_in_db(_reaching_thread, user, app_id)
    actor = _audit_actor(user)

    def delete():
        from functools import partial
        from connections.app_lifecycle import delete_app

        return delete_with_sign_in(app_id, partial(delete_app, app_id, remove_keys=human, audit_actor=actor), _db())

    try:
        result = await remove_app(app_id, lambda: run_in_db(delete), actor=actor)
    except Exception as exc:
        raise _app_error(exc) from None
    try:
        await _run_app_runtime("remove", app_id)
    except Exception:
        # The row is already deleted; report that, and keep the runtime failure in the log.
        log.exception("connected-app runtime remove failed for app_id=%s", app_id)
    return result


@connected_apps_router.post("/servers/{app_id}/reconnect")
async def reconnect_server(app_id: str, user: dict = Depends(require_user)) -> dict[str, object]:
    """Reconnect exactly one reachable app, bounded to one restart per ten seconds."""
    def authorize() -> None:
        from connections.app_reach import get_app

        if is_person_session(user):
            get_app(app_id, db=_db())
            return
        _reaching_thread(user, app_id)

    try:
        await run_in_db(authorize)
        state = await _run_app_runtime("reconnect", app_id)
    except Exception as exc:
        from tools.mcp_runtime import MCPReconnectRateLimitError

        if isinstance(exc, MCPReconnectRateLimitError):
            raise _refusal(str(exc), 429) from None
        raise _app_error(exc) from None
    return {"id": app_id, **state}
