"""Install a validated external skill bundle through workspace guidance history."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Mapping

import yaml
from loguru import logger

from core.skill_sidecar import (
    FORMAT_KEY,
    POINTER_KEY,
    SIDECAR_FILE,
    SIDECAR_FORMAT,
    installed_digest,
)
from core.skill_validation import parse_frontmatter
from infra.guidance_git import GuidanceTarget, SaveResult, save_guidance
from infra.guidance_messages import saver_author

ALREADY_HERE = "already_here"
NOT_INSTALLED = "not_installed"
INSTALLED = "installed"
FAILED = "failed"
_TOOL_GRANTS = frozenset({"allowed-tools", "hooks"})


def _nested(value: object) -> bool:
    return isinstance(value, dict) or (isinstance(value, list) and any(isinstance(item, dict) for item in value))


def _skill_text(name: str, upstream: bytes, description: str) -> str | None:
    """Return an install-safe format-1 SKILL.md, or None for invalid upstream text."""
    try:
        metadata, body = parse_frontmatter(upstream.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    source_metadata = metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}
    safe_metadata = {
        key: value for key, value in source_metadata.items()
        if key not in _TOOL_GRANTS | {POINTER_KEY, FORMAT_KEY} and not _nested(value)
    }
    safe_metadata.update({POINTER_KEY: SIDECAR_FILE, FORMAT_KEY: SIDECAR_FORMAT})
    front = {
        "name": name,
        "description": description,
        **{key: metadata[key] for key in ("license", "compatibility") if key in metadata},
        "metadata": safe_metadata,
    }
    return "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n" + body


def _sidecar(origin: Mapping[str, str]) -> str:
    """The installed sidecar holds only the import origin; an upstream one could carry a handler."""
    return yaml.safe_dump({"origin": dict(origin)}, sort_keys=False, allow_unicode=True)


def _saved(saved: SaveResult) -> dict[str, str]:
    """The item's state, then the save's own outcome and line unchanged, as every guidance save reports them.
    Only ``stale`` (a file appeared at the destination) and ``refused`` write nothing."""
    state = {"stale": ALREADY_HERE, "refused": FAILED}.get(saved.outcome, INSTALLED)
    return {"outcome": state, "history_outcome": saved.outcome, **({"guidance_history": saved.line} if saved.line else {})}


def _mutation_saved(saved: SaveResult, outcome: str) -> dict[str, str]:
    """Return a mutation result without mistaking a no-write save for success."""
    state = outcome if saved.outcome in ("written", "unchanged") else FAILED
    return {"outcome": state, "history_outcome": saved.outcome,
            **({"guidance_history": saved.line} if saved.line else {})}


def _bundle_files(
    name: str,
    upstream_skill_md: bytes,
    other_files: Mapping[str, bytes],
    description: str,
    origin: Mapping[str, str],
) -> dict[str, bytes] | None:
    """Build the rewritten bundle once, rejecting unsafe or ambiguous relative paths."""
    skill_md = _skill_text(name, upstream_skill_md, description)
    if skill_md is None:
        return None
    files = {"SKILL.md": skill_md.encode("utf-8"), SIDECAR_FILE: _sidecar(origin).encode("utf-8")}
    # Names are compared case-folded: on a case-insensitive volume ``Corsia.yaml`` is the sidecar.
    taken = {relative.casefold() for relative in files}
    for relative, data in other_files.items():
        if relative.casefold() == SIDECAR_FILE:
            continue
        path = Path(relative)
        if relative.casefold() in taken or path.is_absolute() or ".." in path.parts:
            return None
        taken.add(relative.casefold())
        files[relative] = data
    installed_origin = {**origin, "installed_digest": installed_digest(files)}
    files[SIDECAR_FILE] = _sidecar(installed_origin).encode("utf-8")
    return files


def _current_files(target: Path) -> dict[str, bytes] | None:
    """Read an installed bundle without following links; hidden OS files do not count as edits."""
    try:
        if not target.is_dir() or target.is_symlink():
            return None
        files: dict[str, bytes] = {}
        for directory, subfolders, names in os.walk(target):
            subfolders[:] = [entry for entry in subfolders if not entry.startswith(".")]
            for entry in subfolders:
                if not stat.S_ISDIR((Path(directory) / entry).lstat().st_mode):
                    return None
            for entry in names:
                if entry.startswith("."):
                    continue
                path = Path(directory) / entry
                if not stat.S_ISREG(path.lstat().st_mode):
                    return None
                files[path.relative_to(target).as_posix()] = path.read_bytes()
        return files
    except OSError:
        return None


def _adopted_at(files: Mapping[str, bytes]) -> str | None:
    """Read the first adoption time from the existing local sidecar."""
    try:
        sidecar = yaml.safe_load(files[SIDECAR_FILE].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, yaml.YAMLError):
        return None
    origin = sidecar.get("origin") if isinstance(sidecar, dict) else None
    adopted_at = origin.get("adopted_at") if isinstance(origin, dict) else None
    return adopted_at if isinstance(adopted_at, str) else None


def _skill_target(
    workspace: Path, name: str, *, scope: str, thread_id: str | None, project_id: str | None, db: Any
) -> tuple[Path, Path] | None:
    """Resolve the owner scope and validate the bundle's direct destination."""
    from core.skill_resolution import skill_scope_root

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        root = skill_scope_root(scope=scope, workspace=workspace, db=db, project_id=project_id, thread_id=thread_id)
    except OSError:
        root = None
    if root is None:
        return None
    skills = root / "skills"
    if not name or Path(name).name != name or skills.is_symlink():
        return None
    return root, skills / name


def install_skill_bundle(
    workspace: Path,
    name: str,
    upstream_skill_md: bytes,
    other_files: Mapping[str, bytes],
    description: str,
    origin: Mapping[str, str],
    *,
    author_thread_id: str,
    consent_decision_id: str | None = None,
    scope: str = "workspace",
    thread_id: str | None = None,
    project_id: str | None = None,
    db: Any = None,
    reason: str = "consented skill import from agent_import",
    binary_safe_history: bool = False,
) -> dict[str, str]:
    """Install one imported skill with no overwrite and a single guidance save; ``outcome`` is
    installed, already_here or failed, plus the save's ``history_outcome`` and ``guidance_history``
    line whenever the save ran."""
    files = _bundle_files(name, upstream_skill_md, other_files, description, origin)
    if files is None:
        logger.warning("skill install failed at bundle rewrite for {}", name)
        return {"outcome": FAILED}
    destination = _skill_target(
        workspace, name, scope=scope, thread_id=thread_id, project_id=project_id, db=db
    )
    if destination is None:
        logger.warning("skill install failed at destination validation for {}", name)
        return {"outcome": FAILED}
    _root, target = destination
    if target.exists() or target.is_symlink():
        return {"outcome": ALREADY_HERE}
    writes = {target / relative: data for relative, data in files.items()}
    target_info = GuidanceTarget(f"skill, {'agent' if scope == 'thread' else scope} scope")
    try:
        saved = save_guidance(
            writes,
            workspace=workspace,
            target=target_info,
            author=saver_author(thread_id=author_thread_id),
            reason=reason,
            evidence=consent_decision_id,
            expected={path: None for path in writes},
            binary_safe_history=binary_safe_history,
        )
    except OSError:
        # A write failed before any commit, so this new folder holds only part of the bundle.
        logger.warning("skill install failed at guidance save for {}", name)
        shutil.rmtree(target, ignore_errors=True)
        return {"outcome": FAILED}
    if saved.outcome not in ("written", "unchanged") or saved.unsynced:
        logger.warning("skill {} guidance save: {} {}", name, saved.outcome, saved.errors or saved.reason)
    return _saved(saved)


def update_skill_bundle(
    workspace: Path,
    name: str,
    upstream_skill_md: bytes,
    other_files: Mapping[str, bytes],
    description: str,
    origin: Mapping[str, str],
    *,
    author_thread_id: str,
    scope: str = "thread",
    thread_id: str | None = None,
    project_id: str | None = None,
    db: Any = None,
    reason: str = "library skill update",
    confirmed_files: Mapping[str, bytes] | None = None,
) -> dict[str, str]:
    """Atomically replace one installed bundle, including files dropped upstream.

    The previous bytes are read immediately before the guidance save and become its
    compare-and-swap expectations.  ``adopted_at`` remains the first local adoption.
    """
    destination = _skill_target(
        workspace, name, scope=scope, thread_id=thread_id, project_id=project_id, db=db
    )
    if destination is None:
        return {"outcome": FAILED}
    _root, target = destination
    previous = dict(confirmed_files) if confirmed_files is not None else _current_files(target)
    adopted_at = _adopted_at(previous) if previous is not None else None
    if previous is None or adopted_at is None:
        return {"outcome": NOT_INSTALLED if not target.exists() else FAILED}
    files = _bundle_files(name, upstream_skill_md, other_files, description, {**origin, "adopted_at": adopted_at})
    if files is None:
        return {"outcome": FAILED}
    writes = {target / relative: data for relative, data in files.items()}
    expected = {
        path: hashlib.sha256(previous[relative]).hexdigest() if relative in previous else None
        for relative, path in ((relative, target / relative) for relative in files)
    }
    deletes = {
        target / relative: hashlib.sha256(data).hexdigest()
        for relative, data in previous.items() if relative not in files
    }
    try:
        saved = save_guidance(
            writes,
            workspace=workspace,
            target=GuidanceTarget(f"skill, {'agent' if scope == 'thread' else scope} scope"),
            author=saver_author(thread_id=author_thread_id),
            reason=reason,
            expected=expected,
            deletes=deletes,
            atomic=True,
            binary_safe_history=True,
        )
    except OSError:
        logger.warning("skill update failed at guidance save for {}", name)
        return {"outcome": FAILED}
    if saved.outcome not in ("written", "unchanged") or saved.unsynced:
        logger.warning("skill {} update guidance save: {} {}", name, saved.outcome, saved.errors or saved.reason)
    return _mutation_saved(saved, "updated")


def remove_skill_bundle(
    workspace: Path,
    name: str,
    *,
    author_thread_id: str,
    scope: str = "thread",
    thread_id: str | None = None,
    project_id: str | None = None,
    db: Any = None,
    reason: str = "library skill removal",
    confirmed_files: Mapping[str, bytes] | None = None,
) -> dict[str, str]:
    """Atomically delete a thread-owned bundle with compare-and-swap expectations."""
    destination = _skill_target(
        workspace, name, scope=scope, thread_id=thread_id, project_id=project_id, db=db
    )
    if destination is None:
        return {"outcome": FAILED}
    _root, target = destination
    previous = dict(confirmed_files) if confirmed_files is not None else _current_files(target)
    if previous is None:
        return {"outcome": NOT_INSTALLED if not target.exists() else FAILED}
    deletes = {target / relative: hashlib.sha256(data).hexdigest() for relative, data in previous.items()}
    try:
        saved = save_guidance(
            {},
            workspace=workspace,
            target=GuidanceTarget(f"skill, {'agent' if scope == 'thread' else scope} scope"),
            author=saver_author(thread_id=author_thread_id),
            reason=reason,
            deletes=deletes,
            atomic=True,
            binary_safe_history=True,
        )
    except OSError:
        logger.warning("skill removal failed at guidance save for {}", name)
        return {"outcome": FAILED}
    if saved.outcome in ("written", "unchanged"):
        for directory in sorted({path.parent for path in deletes}, key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    if saved.outcome not in ("written", "unchanged") or saved.unsynced:
        logger.warning("skill {} removal guidance save: {} {}", name, saved.outcome, saved.errors or saved.reason)
    return _mutation_saved(saved, "removed")


def origin_metadata(
    source: str,
    upstream_skill_md: bytes,
    adopted_at: str,
    version: str | None = None,
    item_manifest: str | None = None,
) -> dict[str, str]:
    """Create the stable origin payload stored beside an imported or library skill."""
    origin = {
        "source": source,
        "sha256": hashlib.sha256(upstream_skill_md).hexdigest(),
        "adopted_at": adopted_at,
    }
    if version is not None:
        origin["version"] = version
    if item_manifest is not None:
        origin["item_manifest"] = item_manifest
    return origin
