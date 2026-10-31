"""`skill_library`: discover and manage reviewed thread-local library skills."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from infra.db_async import run_off_db
from loguru import logger
from tools.base import Tool
from tools.skill_library_common import (
    LibraryIndexError as _LibraryIndexError,
    changed_paths as _changed_paths,
    confirmed_bundle as _confirmed_bundle,
    current_library_item as _current_library_item,
    discard_confirmation as _discard_confirmation,
    emit_action as _emit_action,
    effective as _effective,
    index as _index,
    library_origin as _library_origin,
    local_edits_token as _local_edits_token,
    persistent_owner as _persistent_owner,
    same_library_item as _same_library_item,
    write_result as _write_result,
)


from tools.skill_library_schema import DESCRIPTION as _DESCRIPTION, parameters as _parameters


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
        return _parameters()

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
    from infra.user_profile import uses_community_improvements

    db = get_db()
    try:
        try:
            if action not in {"search", "install", "update", "remove", "fetch_pack"}:
                result = _error("invalid_action", "action must be search, install, update, remove, or fetch_pack")
            elif action in {"search", "install", "update", "fetch_pack"} and not uses_community_improvements(_workspace()):
                result = _error(
                    "community_off",
                    "The person hasn't agreed to use the community's improvements (the 'Use community improvements' line in their profile), so the library is off and nothing was searched, installed or updated. Continue without it, and write your own skill with skill skill-creator if you need one. Only the person changes this answer; don't ask them for it just for this task. Their answer to the setup decision titled 'Use and share improvements with the CORSIA community?' turns the library on. It is raised unchanged from setup.setup_decisions, during first-run setup only. remove still works on a copy you already hold.",
                )
            else:
                owner_id = _persistent_owner(thread_id, db)
                project_id = thread.get("project_id")
                if action == "search":
                    result = (_error("query_required", "search requires query; pass a few words describing the job")
                              if not isinstance(query, str) or not query.strip()
                              else _search(query, owner_id, project_id, db, thread_id=thread_id))
                elif not isinstance(name, str) or not name.strip():
                    result = _error("name_required", f"{action} requires name from a search result or your skills index")
                elif action == "fetch_pack":
                    result = _fetch_pack(name, thread_id, project_id, db)
                elif action == "install":
                    result = _install(name, owner_id, project_id, db)
                elif action == "update":
                    result = _update(name, owner_id, project_id, db, discard_local_edits, local_edits_token)
                else:
                    result = _remove(name, owner_id, project_id, db, discard_local_edits, local_edits_token)
        except _LibraryIndexError as error:
            result = _error(error.code, error.message)
        _emit_action(action, thread_id, result)
        return result
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


def _search(query: str, owner_id: str, project_id: str | None, db, *, thread_id: str | None = None) -> dict[str, Any]:
    from library.items import search_items, source_ref
    from library.pack_fetch import fetched_pack_versions, pack_state

    index = _index()
    origins = _installed_library_origins(owner_id, db)
    items = search_items(index.items, query)
    pack_versions = fetched_pack_versions(thread_id or owner_id, db) if any(item.get("kind") == "pack" for item in items) else {}
    results = []
    for item in items:
        kind = item.get("kind", "skill")
        result = {
            "name": item["name"], "description": item["description"], "version": item["version"],
            "license": item.get("license"), "source": source_ref(item), "files": len(item["files"]),
            "state": _state(item, origins) if kind == "skill" else pack_state(item, pack_versions),
        }
        if kind == "pack":
            result["kind"] = "pack"
        results.append(result)
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
    return index, find_item(index.items, name)


def _fetch_pack(name: str, thread_id: str, project_id: str | None, db) -> dict[str, Any]:
    from library.pack_fetch import fetch_pack
    index, item = _item(name)
    if item is None:
        return _error("not_in_library", "search first; names are exact")
    result = fetch_pack(item, thread_id, project_id, _workspace(), db)
    result["index_age"] = index.index_age
    return result


def _find_local(name: str, owner_id: str, project_id: str | None, db):
    _effective_rows, local = _effective(owner_id, project_id, _workspace(), db)
    return next((row for row in local if row.name == name), None)


def _install(name: str, owner_id: str, project_id: str | None, db) -> dict[str, Any]:
    from infra.skill_install import install_skill_bundle, origin_metadata
    from library.items import LibraryIntegrityError, LibraryItemUnavailableError, LibraryMainChangedError, fetch_bundle, item_manifest

    index, item = _item(name)
    if item is None:
        return _error("not_in_library", "search first; names are exact")
    if item["withdrawn"]:
        return _error("withdrawn", f"{item['withdrawn_reason']}; remove takes out a copy you already hold")
    if item.get("kind", "skill") == "pack":
        return _error("pack_requires_fetch", "use fetch_pack to fetch a blueprint pack")
    effective, local_rows = _effective(owner_id, project_id, _workspace(), db)
    conflict = next((row for row in effective if row.name.casefold() == name.casefold()), None)
    if conflict is not None:
        origin = _library_origin(conflict)
        if origin and _same_library_item(origin, item):
            if any(row.path == conflict.path for row in local_rows):
                registration_error = _ensure_registered(owner_id, name, project_id, db)
                if registration_error is not None:
                    return registration_error
            return _error("already_installed", f"{name} is already installed at {conflict.path} in {conflict.scope} scope")
        return _error("name_taken", f"{name} is already provided at {conflict.path}; the library item was not installed")
    try:
        bundle = fetch_bundle(item)
    except LibraryMainChangedError as error:
        return _error("integrity", str(error))
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
        registration_error = _ensure_registered(owner_id, name, project_id, db)
        if registration_error is not None:
            return registration_error
        return {"outcome": "installed", "name": name, "version": item["version"], "path": _path(owner_id, project_id, db, name), "files": len(item["files"])}
    return _write_result(result)


def _update(
    name: str, owner_id: str, project_id: str | None, db, discard: bool, local_edits_token: object,
) -> dict[str, Any]:
    from infra.skill_install import origin_metadata, update_skill_bundle
    from library.installed import local_bundle, local_edits
    from library.items import LibraryIntegrityError, LibraryItemUnavailableError, LibraryMainChangedError, fetch_bundle, item_manifest

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
    except LibraryMainChangedError as error:
        return _error("integrity", str(error))
    except LibraryIntegrityError as error:
        return _error("integrity", f"{error}; report it to the conductor and do not retry")
    except LibraryItemUnavailableError as error:
        return _error("library_unreachable", f"{error}; continue without it and try again later")
    skill_md = bundle.pop("SKILL.md")
    current, _problems = local_bundle(folder)
    changed_files = _changed_paths(current, name, skill_md, item["description"], bundle)
    result = update_skill_bundle(
        _workspace(), name, skill_md, bundle, item["description"],
        origin_metadata(_source(item), skill_md, version=item["version"], item_manifest=item_manifest(item)),
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
        except ValueError as error:
            logger.warning("library artifact release failed for {}/{}: {}", owner_id, name, error)
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


def _ensure_registered(owner_id: str, name: str, project_id: str | None, db) -> dict[str, str] | None:
    """Repair an interrupted install's missing registration on every idempotent retry."""
    try:
        _register(owner_id, name, project_id, db)
    except (OSError, ValueError) as error:
        logger.warning("library skill registration failed for {}/{}: {}", owner_id, name, error)
        return _error("registration_failed", "the skill copy is installed but unavailable; retry install to register it")
    return None


def _path(owner_id: str, project_id: str | None, db, name: str) -> str:
    from core.skill_resolution import skill_scope_root

    root = skill_scope_root(scope="thread", thread_id=owner_id, project_id=project_id, workspace=_workspace(), db=db)
    assert root is not None
    return str(root / "skills" / name / "SKILL.md")


def _error(code: str, message: str) -> dict[str, str]:
    return {"error": code, "message": message}
