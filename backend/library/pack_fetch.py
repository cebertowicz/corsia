"""Fetch and register a verified blueprint for its designated coordinating agent."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from events.connectors.library import INSTALLED_PACK_TYPE, LIBRARY_INTEGRATION
from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator
from library.producer import INSTALLED_ROWS_SQL
from registry.models import SURFACING_QUIET

_PACK_REGISTRATION_SQL = INSTALLED_ROWS_SQL + """ AND identities.external_id=? AND identities.path=?
    AND registrations.parent_thread_id=? AND registrations.surfacing=? LIMIT 1"""

logger = logging.getLogger(__name__)


def can_fetch(thread_id: str, project_id: str | None, db) -> bool:
    """Check the current coordinating agent designation for this scope."""
    return get_scope_coordinator(project_id or WORKSPACE_SCOPE, db=db) == thread_id


def fetch_pack(item: dict[str, Any], thread_id: str, project_id: str | None, workspace: Path, db) -> dict[str, Any]:
    """Verify, publish and register the current indexed blueprint."""
    from core.skill_resolution import skill_scope_root
    from core.workspace_scaffolding import AtomicRenameUnavailableError
    from library.items import LibraryIntegrityError, LibraryItemError, LibraryItemUnavailableError, fetch_bundle
    from library.pack_publish import publish_pack
    from library.pack_validation import validate_pack
    from registry.models import update_surfacing
    from registry.registration_writer import register_artifact

    if not can_fetch(thread_id, project_id, db):
        return {"error": "not_conductor", "message": "Only the designated project conductor or workspace CORSIA agent can fetch a blueprint. Ask the conductor; if none exists, continue without the pack."}
    if item.get("kind") != "pack" or item.get("pack_kind") != "blueprint":
        return {"error": "not_blueprint", "message": "Only blueprint packs can be fetched; install a single skill with install instead."}
    if item["withdrawn"]:
        return {"error": "withdrawn", "message": f"{item['withdrawn_reason']}; do not adopt it, and search for another blueprint."}
    try:
        files = fetch_bundle(item)
        validate_pack(item, files)
    except LibraryItemUnavailableError as error:
        return {"error": "library_unreachable", "message": f"{error}; continue without the pack and try again later"}
    except (LibraryIntegrityError, LibraryItemError) as error:
        return {"error": "integrity", "message": f"{error}; nothing was saved, so report it to the conductor and do not retry"}
    root = skill_scope_root(scope="thread", thread_id=thread_id, project_id=project_id, workspace=workspace, db=db)
    if root is None:
        return {"error": "scope_unavailable", "message": "This thread has no project folder to save the pack in; fetch it from a thread inside the project."}
    try:
        root.mkdir(parents=True, exist_ok=True)
        dest = publish_pack(root, item["name"], item["version"], files)
    except FileExistsError:
        dest = root / "packs" / item["name"] / item["version"]
        if not _matches_published(dest, files):
            return {"error": "copy_differs", "path": str(dest), "message": f"The folder at {dest} does not match the published bytes. Remove that folder to allow a fresh fetch."}
        if _registered(thread_id, item["name"], item["version"], dest, db):
            return {"outcome": "already_fetched", "name": item["name"], "version": item["version"], "path": str(dest)}
    except (OSError, ValueError, AtomicRenameUnavailableError):
        logger.exception("blueprint publication failed for %s@%s", item["name"], item["version"])
        return {"error": "publication_failed", "message": "The pack could not be saved in the project's packs folder, and nothing was adopted; continue without it and try the fetch again later."}
    try:
        artifact = register_artifact(integration=LIBRARY_INTEGRATION, type=INSTALLED_PACK_TYPE, external_id=f"{thread_id}/{item['name']}/{item['version']}", parent_thread_id=thread_id, path=str(dest / "manifest.yaml"), db=db)
        update_surfacing(artifact["id"], surfacing=SURFACING_QUIET, parent_thread_id=thread_id, actor_thread_id=thread_id, db=db)
    except (OSError, ValueError, sqlite3.Error):
        logger.exception("blueprint registration failed for %s@%s", item["name"], item["version"])
        return {"error": "registration_failed", "message": f"The verified pack is saved at {dest} but not yet registered; do not change that folder, and fetch again to register it."}
    return {"outcome": "fetched", "name": item["name"], "version": item["version"], "path": str(dest), "next": "Read manifest.yaml at the returned path and propose to the person which parts to adopt, never all of it."}


def fetched_pack_versions(thread_id: str, db) -> dict[str, set[str]]:
    rows = db.execute(INSTALLED_ROWS_SQL + " AND registrations.parent_thread_id=?",
                      (LIBRARY_INTEGRATION, INSTALLED_PACK_TYPE, thread_id)).fetchall()
    versions: dict[str, set[str]] = {}
    for row in rows:
        external_id = row["external_id"]
        if not isinstance(external_id, str):
            continue
        parts = external_id.rsplit("/", 2)
        if len(parts) == 3 and parts[0] == thread_id:
            versions.setdefault(parts[1], set()).add(parts[2])
    return versions


def pack_state(item: dict[str, Any], versions: dict[str, set[str]]) -> str:
    import semver

    held = versions.get(item["name"], set())
    if item["version"] in held:
        return "fetched"
    current = semver.Version.parse(item["version"])
    parsed = []
    for version in held:
        try:
            parsed.append(semver.Version.parse(version))
        except ValueError:
            continue
    if parsed and max(parsed) < current:
        return "update_available"
    return "not_fetched"


def _registered(owner: str, name: str, version: str, folder: Path, db) -> bool:
    """An active quiet registration completes publication; incomplete state can be repaired."""
    return db.execute(
        _PACK_REGISTRATION_SQL,
        (LIBRARY_INTEGRATION, INSTALLED_PACK_TYPE, f"{owner}/{name}/{version}",
         str(folder / "manifest.yaml"), owner, SURFACING_QUIET),
    ).fetchone() is not None


def _matches_published(folder: Path, expected: dict[str, bytes]) -> bool:
    """Only repair registration when every existing entry matches verified bytes."""
    try:
        mode = folder.lstat().st_mode
        if not stat.S_ISDIR(mode):
            return False
        found: dict[str, bytes] = {}
        for root, directories, names in os.walk(folder, followlinks=False):
            for name in directories:
                mode = (Path(root) / name).lstat().st_mode
                if not stat.S_ISDIR(mode):
                    return False
            for name in names:
                path = Path(root) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    return False
                found[path.relative_to(folder).as_posix()] = path.read_bytes()
        return found == expected
    except OSError:
        return False
