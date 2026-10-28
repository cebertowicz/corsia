"""Fetch and register a verified blueprint for its designated coordinating agent."""

from __future__ import annotations

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
    from registry.models import SURFACING_QUIET, update_surfacing
    from registry.registration_writer import register_artifact

    if not can_fetch(thread_id, project_id, db):
        return {"error": "not_conductor", "message": "Only the designated project conductor or workspace CORSIA agent can fetch a blueprint. Ask the conductor; if none exists, continue without the pack."}
    if item.get("kind") != "pack" or item.get("pack_kind") != "blueprint":
        return {"error": "not_blueprint", "message": "Only blueprint packs can be fetched."}
    if item["withdrawn"]:
        return {"error": "withdrawn", "message": item["withdrawn_reason"]}
    try:
        files = fetch_bundle(item)
        validate_pack(item, files)
    except LibraryItemUnavailableError as error:
        return {"error": "library_unreachable", "message": str(error)}
    except (LibraryIntegrityError, LibraryItemError) as error:
        return {"error": "integrity", "message": str(error)}
    root = skill_scope_root(scope="thread", thread_id=thread_id, project_id=project_id, workspace=workspace, db=db)
    if root is None:
        return {"error": "scope_unavailable", "message": "The thread's project root is unavailable."}
    try:
        root.mkdir(parents=True, exist_ok=True)
        dest = publish_pack(root, item["name"], item["version"], files)
    except FileExistsError:
        dest = root / "packs" / item["name"] / item["version"]
        if not _matches_published(dest, files) or _registered(thread_id, item["name"], item["version"], dest, db):
            return {"error": "already_exists", "message": "This pack version already exists and was not overwritten."}
    except (OSError, ValueError, AtomicRenameUnavailableError) as error:
        return {"error": "publication_failed", "message": str(error)}
    try:
        artifact = register_artifact(integration=LIBRARY_INTEGRATION, type=INSTALLED_PACK_TYPE, external_id=f"{thread_id}/{item['name']}/{item['version']}", parent_thread_id=thread_id, path=str(dest / "manifest.yaml"), db=db)
        update_surfacing(artifact["id"], surfacing=SURFACING_QUIET, parent_thread_id=thread_id, actor_thread_id=thread_id, db=db)
    except (OSError, ValueError, sqlite3.Error) as error:
        return {"error": "registration_failed", "message": f"The verified pack is present at {dest}, but registration failed: {error}. Do not overwrite it; registration needs repair."}
    return {"outcome": "fetched", "name": item["name"], "version": item["version"], "path": str(dest), "next": "Read the pack with conductor-bootstrap and propose the parts to adopt."}


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
        if not stat.S_ISDIR(mode) or mode & 0o222:
            return False
        found: dict[str, bytes] = {}
        for root, directories, names in os.walk(folder, followlinks=False):
            for name in directories:
                mode = (Path(root) / name).lstat().st_mode
                if not stat.S_ISDIR(mode) or mode & 0o222:
                    return False
            for name in names:
                path = Path(root) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_nlink != 1:
                    return False
                found[path.relative_to(folder).as_posix()] = path.read_bytes()
        return found == expected
    except OSError:
        return False
