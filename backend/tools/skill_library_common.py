"""Shared state, identity, and confirmation helpers for the library tool."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class LibraryIndexError(Exception):
    """The agent-facing form of a raw-index failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


def persistent_owner(thread_id: str, db) -> str:
    """Resolve a caller to its persistent thread owner."""
    from core.skill_resolution import thread_ancestry

    return thread_ancestry(thread_id, db)[0]


def index():
    """Load the raw-only library index as an actionable tool-level result."""
    from library import LibraryUnreachableError, load_index
    from library.index import LibraryIndexValidationError

    try:
        return load_index()
    except LibraryUnreachableError as error:
        raise LibraryIndexError(
            "library_unreachable", "the library index is unavailable; continue without it and try again later",
        ) from error
    except LibraryIndexValidationError as error:
        raise LibraryIndexError(
            "library_invalid", "the library index is malformed; report it to the conductor and do not retry",
        ) from error


def effective(owner_id: str, project_id: str | None, workspace: Path, db):
    """Return effective and local skills for one persistent owner."""
    from core.skill_resolution import local_skill_inventory, resolve_effective_skills

    resolution = resolve_effective_skills(
        thread_id=owner_id, project_id=project_id, workspace=workspace, db=db,
    )
    local = local_skill_inventory(
        scope="thread", thread_id=owner_id, project_id=project_id, workspace=workspace, db=db,
    )
    return resolution.included, local


def library_origin(row) -> dict[str, Any] | None:
    """Return a copied library-origin mapping from one local skill row."""
    origin = row.corsia.get("origin") if isinstance(row.corsia, dict) else None
    return dict(origin) if isinstance(origin, dict) and isinstance(origin.get("source"), str) else None


def same_library_item(origin: dict[str, Any], item: dict[str, Any]) -> bool:
    """Whether one origin belongs to the same named upstream library item."""
    source = origin.get("source")
    prefix = f"{item['source']['repository']}@"
    suffix = f":{item['source']['path']}"
    return isinstance(source, str) and source.startswith(prefix) and source.endswith(suffix)


def current_library_item(origin: dict[str, Any], item: dict[str, Any]) -> bool:
    """Whether origin records the current source and declared item manifest."""
    from library.items import item_manifest, source_ref

    return origin.get("source") == source_ref(item) and origin.get("item_manifest") == item_manifest(item)


def bundle_token(owner_id: str, name: str, files: dict[str, bytes], problems: list[str]) -> str:
    """Bind edit-discard confirmation to one complete visible bundle snapshot."""
    digest = hashlib.sha256()
    digest.update(owner_id.encode() + b"\0" + name.encode() + b"\0")
    for relative in sorted(files):
        digest.update(relative.encode() + b"\0" + hashlib.sha256(files[relative]).digest())
    for problem in sorted(problems):
        digest.update(b"!" + problem.encode() + b"\0")
    return digest.hexdigest()


def local_edits_token(owner_id: str, name: str, folder: Path) -> str | None:
    """Return the confirmation token for the current visible bundle."""
    from library.installed import local_bundle

    files, problems = local_bundle(folder)
    return bundle_token(owner_id, name, files, problems) if files is not None else None


def confirmed_bundle(owner_id: str, name: str, folder: Path, token: object) -> dict[str, bytes] | None:
    """Return the token-confirmed bytes for the mutation writer's CAS snapshot."""
    from library.installed import local_bundle

    files, problems = local_bundle(folder)
    if files is not None and isinstance(token, str) and token == bundle_token(owner_id, name, files, problems):
        return files
    return None


def discard_confirmation(
    owner_id: str, name: str, folder: Path, paths: list[str], token: object,
) -> dict[str, Any]:
    """Return the actionable refusal when current bytes no longer match the token."""
    current = local_edits_token(owner_id, name, folder)
    if confirmed_bundle(owner_id, name, folder, token) is not None:
        return {}
    return {
        "error": "discard_confirmation_required",
        "message": "review the changed files first; repeat with discard_local_edits: true and the local_edits_token from that response",
        "changed_files": paths,
        "local_edits_token": current,
    }


def changed_paths(current: dict[str, bytes] | None, skill_md: bytes, bundle: dict[str, bytes]) -> list[str]:
    """Name both arriving and upstream-dropped visible files in an update result."""
    new_paths = {"SKILL.md", *bundle}
    dropped = (set(current or {}) - new_paths) - {"corsia.yaml"}
    skill_changed = current is None or current.get("SKILL.md") != skill_md
    return sorted(set(bundle) | dropped | ({"SKILL.md"} if skill_changed else set()))
