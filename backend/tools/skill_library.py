"""`skill_library`: discover and manage reviewed thread-local library skills."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from infra.db_async import run_off_db
from tools.base import Tool
from tools.skill_library_common import (
    LibraryIndexError as _LibraryIndexError,
    changed_paths as _changed_paths,
    confirmed_bundle as _confirmed_bundle,
    current_library_item as _current_library_item,
    discard_confirmation as _discard_confirmation,
    effective as _effective,
    index as _index,
    library_origin as _library_origin,
    local_edits_token as _local_edits_token,
    persistent_owner as _persistent_owner,
    same_library_item as _same_library_item,
)


_DESCRIPTION = (
    "Find and install reviewed skills from the public CORSIA library, and keep your installed copies current. "
    "Check your own skills index first, including its names-only line; search here only when nothing there fits. "
    "`search` takes `query`. `install`, `update` and `remove` take the `name` from a search result. "
    "Installing puts a copy in your own thread (a temporary worker's goes to its persistent parent), and you can "
    "load it right away with `read_file`. If the library's version changes or the item is withdrawn, you get a notice. "
    "Nothing changes your copy unless you call `update` or `remove`."
)



def _workspace() -> Path:
    from infra.config import WORKSPACE

    return Path(WORKSPACE)


class SkillLibraryTool(Tool):
    @property
    def name(self) -> str:
        return "skill_library"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "install", "update", "remove"],
                    "description": "search reviewed skills, install one, update your copy, or remove your copy",
                },
                "query": {
                    "type": "string",
                    "description": "search only: words describing the job to do",
                },
                "name": {
                    "type": "string",
                    "description": "install, update, or remove only: exact name from a search result or your skill index",
                },
                "discard_local_edits": {
                    "type": "boolean",
                    "default": False,
                    "description": "update/remove only: set true only with the local_edits_token returned after reviewing affected files",
                },
                "local_edits_token": {
                    "type": "string",
                    "description": "update/remove only: exact token returned by local_edits; it confirms the listed edits may be discarded",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        from tools.delegation import _require_context

        context = _require_context(kwargs)
        if isinstance(context, str):
            return json.dumps({"error": "context_required", "message": context})
        thread_id, thread = context
        return json.dumps(await run_off_db(
            _execute, action, thread_id, thread, kwargs.get("query"), kwargs.get("name"),
            bool(kwargs.get("discard_local_edits", False)), kwargs.get("local_edits_token"),
        ), sort_keys=True)


def _execute(
    action: str,
    thread_id: str,
    thread: dict[str, Any],
    query: object,
    name: object,
    discard_local_edits: bool,
    local_edits_token: object = None,
) -> dict[str, Any]:
    """Run the complete operation off the event loop; writes use canonical owners."""
    from infra.database import get_db

    db = get_db()
    try:
        owner_id = _persistent_owner(thread_id, db)
        project_id = thread.get("project_id")
        try:
            if action == "search":
                if not isinstance(query, str) or not query.strip():
                    return _error("query_required", "search requires query; pass a few words describing the job")
                return _search(query, owner_id, project_id, db)
            if not isinstance(name, str) or not name.strip():
                return _error("name_required", f"{action} requires name from a search result or your skills index")
            if action == "install":
                return _install(name, owner_id, project_id, db)
            if action == "update":
                return _update(name, owner_id, project_id, db, discard_local_edits, local_edits_token)
            if action == "remove":
                return _remove(name, owner_id, project_id, db, discard_local_edits, local_edits_token)
            return _error("invalid_action", "action must be search, install, update, or remove")
        except _LibraryIndexError as error:
            return _error(error.code, error.message)
    finally:
        db.close()



def _installed_library_origins(owner_id: str, db) -> dict[str, dict[str, Any]]:
    """Read this owner's installed library copies with one indexed relation query."""
    from events.connectors.library import INSTALLED_SKILL_TYPE, LIBRARY_INTEGRATION
    from library.installed import read_origin

    rows = db.execute(
        """SELECT identities.external_id, identities.path
             FROM artifact_identities AS identities
             JOIN artifact_registrations AS registrations ON registrations.artifact_id=identities.id
             WHERE identities.integration=? AND identities.type=? AND identities.state='active'
               AND registrations.parent_thread_id=?
               AND registrations.relationship_state IN ('owner-active', 'owner-pinned')""",
        (LIBRARY_INTEGRATION, INSTALLED_SKILL_TYPE, owner_id),
    ).fetchall()
    origins: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = row["path"]
        external_id = row["external_id"]
        if not isinstance(path, str) or not isinstance(external_id, str):
            continue
        origin = read_origin(Path(path).parent)
        if origin is not None:
            origins[external_id.rpartition("/")[2]] = origin
    return origins


def _state(item: dict[str, Any], origins: dict[str, dict[str, Any]]) -> str:
    origin = origins.get(item["name"])
    if origin and _same_library_item(origin, item):
        return "installed" if _current_library_item(origin, item) else "update_available"
    return "not_installed"


def _source(item: dict[str, Any]) -> str:
    from library.items import source_ref

    return source_ref(item)


def _search(query: str, owner_id: str, project_id: str | None, db) -> dict[str, Any]:
    from library.items import search_items, source_ref

    index = _index()
    from library.producer import request_refresh

    request_refresh()
    origins = _installed_library_origins(owner_id, db)
    results = []
    for item in search_items(index.skills, query):
        results.append({
            "name": item["name"], "description": item["description"], "version": item["version"],
            "license": item["license"], "source": source_ref(item), "files": len(item["files"]),
            "state": _state(item, origins),
        })
    if not results:
        return {
            "index_age": index.index_age,
            "results": [],
            "hint": "nothing in the library fits; write your own with skill skill-creator",
        }
    return {"index_age": index.index_age, "results": results}


def _item(name: str):
    from library.items import find_item

    index = _index()
    return index, find_item(index.skills, name)


def _find_local(name: str, owner_id: str, project_id: str | None, db):
    _effective_rows, local = _effective(owner_id, project_id, _workspace(), db)
    return next((row for row in local if row.name == name), None)


def _install(name: str, owner_id: str, project_id: str | None, db) -> dict[str, Any]:
    from infra.skill_install import install_skill_bundle, origin_metadata
    from library.items import LibraryIntegrityError, LibraryItemUnavailableError, fetch_bundle, item_manifest

    index, item = _item(name)
    if item is None:
        return _error("not_in_library", "search first; names are exact")
    if item["withdrawn"]:
        return _error("withdrawn", f"{item['withdrawn_reason']}; remove takes out a copy you already hold")
    effective, _local = _effective(owner_id, project_id, _workspace(), db)
    conflict = next((row for row in effective if row.name == name), None)
    if conflict is not None:
        origin = _library_origin(conflict)
        if origin and _same_library_item(origin, item):
            return _error("already_installed", f"{name} is already installed at {conflict.path} in {conflict.scope} scope")
        return _error("name_taken", f"{name} is already provided at {conflict.path}; the library item was not installed")
    try:
        bundle = fetch_bundle(item)
    except LibraryIntegrityError as error:
        return _error("integrity", f"{error}; report it to the conductor and do not retry")
    except LibraryItemUnavailableError as error:
        return _error("library_unreachable", f"{error}; continue without it and try again later")
    skill_md = bundle.pop("SKILL.md")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    result = install_skill_bundle(
        _workspace(), name, skill_md, bundle, item["description"],
        origin_metadata(_source(item), skill_md, now, item["version"], item_manifest(item)),
        author_thread_id=owner_id, scope="thread", thread_id=owner_id, project_id=project_id, db=db,
        reason="reviewed library skill install", binary_safe_history=True,
    )
    if result["outcome"] == "installed":
        _register(owner_id, name, project_id, db)
        return {"outcome": "installed", "name": name, "version": item["version"], "path": _path(owner_id, project_id, db, name), "files": len(item["files"])}
    return _write_result(result)


def _update(
    name: str, owner_id: str, project_id: str | None, db, discard: bool, local_edits_token: object,
) -> dict[str, Any]:
    from infra.skill_install import origin_metadata, update_skill_bundle
    from library.installed import local_bundle, local_edits
    from library.items import LibraryIntegrityError, LibraryItemUnavailableError, fetch_bundle, item_manifest

    _index_result, item = _item(name)
    if item is None:
        return _error("not_in_library", "search first; names are exact")
    local = _find_local(name, owner_id, project_id, db)
    if local is None:
        return _not_installed(name, owner_id, project_id, db)
    origin = _library_origin(local)
    if origin is None or not _same_library_item(origin, item):
        return _error("not_installed", f"{name} is not a library copy owned by this thread")
    if item["withdrawn"]:
        return _error("withdrawn", f"{item['withdrawn_reason']}; remove takes out your installed copy")
    if _current_library_item(origin, item):
        return {"outcome": "up_to_date", "name": name, "version": item["version"]}
    folder = Path(local.path).parent
    edited, local_paths = local_edits(folder, origin)
    if edited and not discard:
        return {
            "error": "local_edits",
            "message": f"your changed files: {', '.join(local_paths) or 'skill folder'}; review them before choosing whether to discard them",
            "changed_files": local_paths,
            "local_edits_token": _local_edits_token(owner_id, name, folder),
        }
    confirmed_files = None
    if edited and discard:
        confirmed_files = _confirmed_bundle(owner_id, name, folder, local_edits_token)
        if confirmed_files is None:
            return _discard_confirmation(owner_id, name, folder, local_paths, local_edits_token)
    try:
        bundle = fetch_bundle(item)
    except LibraryIntegrityError as error:
        return _error("integrity", f"{error}; report it to the conductor and do not retry")
    except LibraryItemUnavailableError as error:
        return _error("library_unreachable", f"{error}; continue without it and try again later")
    skill_md = bundle.pop("SKILL.md")
    current, _problems = local_bundle(folder)
    changed_files = _changed_paths(current, skill_md, bundle)
    result = update_skill_bundle(
        _workspace(), name, skill_md, bundle, item["description"],
        origin_metadata(_source(item), skill_md, "ignored", item["version"], item_manifest(item)),
        author_thread_id=owner_id, thread_id=owner_id, project_id=project_id, db=db,
        reason="reviewed library skill update", confirmed_files=confirmed_files,
    )
    if result["outcome"] == "updated":
        return {
            "outcome": "updated", "name": name, "from": origin.get("version"), "to": item["version"],
            "changed_files": changed_files,
        }
    return _write_result(result)


def _remove(
    name: str, owner_id: str, project_id: str | None, db, discard: bool, local_edits_token: object,
) -> dict[str, Any]:
    from infra.skill_install import remove_skill_bundle
    from library.installed import local_edits
    from registry.ownership import release_artifact

    local = _find_local(name, owner_id, project_id, db)
    if local is None:
        return _not_installed(name, owner_id, project_id, db)
    origin = _library_origin(local)
    if origin is None:
        return _error("not_installed", f"{name} is not a library copy owned by this thread")
    folder = Path(local.path).parent
    edited, local_paths = local_edits(folder, origin)
    if edited and not discard:
        return {
            "error": "local_edits",
            "message": f"your changed files: {', '.join(local_paths) or 'skill folder'}; review them before choosing whether to discard them",
            "changed_files": local_paths,
            "local_edits_token": _local_edits_token(owner_id, name, folder),
        }
    confirmed_files = None
    if edited and discard:
        confirmed_files = _confirmed_bundle(owner_id, name, folder, local_edits_token)
        if confirmed_files is None:
            return _discard_confirmation(owner_id, name, folder, local_paths, local_edits_token)
    result = remove_skill_bundle(
        _workspace(), name, author_thread_id=owner_id, thread_id=owner_id, project_id=project_id, db=db,
        reason="reviewed library skill removal", confirmed_files=confirmed_files,
    )
    if result["outcome"] == "removed":
        try:
            release_artifact(
                integration="library", type="installed-skill", external_id=f"{owner_id}/{name}",
                actor_thread_id=owner_id, authority="library", reason="library skill removed",
                source_path="tools.skill_library", db=db,
            )
        except ValueError:
            pass
        return {"outcome": "removed", "name": name}
    return _write_result(result)


def _not_installed(name: str, owner_id: str, project_id: str | None, db) -> dict[str, str]:
    effective, _local = _effective(owner_id, project_id, _workspace(), db)
    other = next((row for row in effective if row.name == name), None)
    if other is not None:
        return _error("not_installed", f"{name} is only in {other.scope} scope at {other.path}; its owner changes it")
    return _error("not_installed", f"{name} is not installed in this thread")


def _register(owner_id: str, name: str, project_id: str | None, db) -> None:
    from events.connectors.library import INSTALLED_SKILL_TYPE, LIBRARY_INTEGRATION
    from registry.models import SURFACING_QUIET, update_surfacing
    from registry.registration_writer import register_artifact

    artifact = register_artifact(
        integration=LIBRARY_INTEGRATION, type=INSTALLED_SKILL_TYPE, external_id=f"{owner_id}/{name}",
        parent_thread_id=owner_id, path=_path(owner_id, project_id, db, name), db=db,
    )
    update_surfacing(artifact["id"], surfacing=SURFACING_QUIET, parent_thread_id=owner_id, actor_thread_id=owner_id, db=db)


def _path(owner_id: str, project_id: str | None, db, name: str) -> str:
    from core.skill_resolution import skill_scope_root

    root = skill_scope_root(scope="thread", thread_id=owner_id, project_id=project_id, workspace=_workspace(), db=db)
    assert root is not None
    return str(root / "skills" / name / "SKILL.md")


def _write_result(result: dict[str, str]) -> dict[str, Any]:
    if result.get("history_outcome") == "history-off":
        return _error("history_off", "guidance history is off for this folder, so nothing changed; turn history back on first")
    return _error("invalid_item", "the library item could not be written without changing your existing skills")


def _error(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}
