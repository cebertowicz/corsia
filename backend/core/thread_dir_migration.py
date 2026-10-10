"""Direct compatibility migration into the canonical hidden thread layout.

Only the currently observed layouts are supported:

* ``working/<id>`` is routed directly into ``.threads/<id>``;
* ``threads/<id>`` is moved as its already-structured directory.

This module deliberately has no generalized migration registry.  Its callers
provide DB-known session IDs, dry-runs validate exactly what apply would move,
and the entire supplied scope is preflighted before a single source changes.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from core.workspace_layout import resolve_thread_root, resolve_workspace_root


_SKILLS = "skills"
_CONTEXT = "context"
_WORKING = "working"


@dataclass
class MigrationResult:
    """Count the direct migration actions planned or completed for one scope."""

    moved_skills: int = 0
    moved_context: int = 0
    moved_working: int = 0
    threads_migrated: list[str] = field(default_factory=list)

    def merge(self, other: "MigrationResult") -> None:
        """Add another scope or thread result to this aggregate."""
        self.moved_skills += other.moved_skills
        self.moved_context += other.moved_context
        self.moved_working += other.moved_working
        self.threads_migrated.extend(other.threads_migrated)


@dataclass(frozen=True)
class _PlannedMove:
    source: Path
    destination: Path
    source_layout: str
    durable_paths: frozenset[str]
    result: MigrationResult


def _thread_dest_relpath(rel_posix: str, durable: Collection[str]) -> str:
    """Route one legacy ``working/<id>`` file below ``.threads/<id>``."""
    if rel_posix.split("/", 1)[0] == _SKILLS:
        return rel_posix
    if rel_posix in durable:
        return f"{_CONTEXT}/{rel_posix}"
    return f"{_WORKING}/{rel_posix}"


def migrate_scope(
    scope_root: Path,
    *,
    thread_ids: Collection[str] | None = None,
    durable_by_thread: dict[str, set[str]] | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """Directly migrate known legacy thread directories in one scope.

    ``thread_ids`` is mandatory: only session IDs obtained from the database are
    eligible.  A destination by itself is an idempotent no-op.  Any destination
    plus legacy source, both legacy sources, malformed identity, or symlinked
    source/target rejects the whole scope before mutation.
    """
    known_ids = _known_thread_ids(thread_ids)
    scope = resolve_workspace_root(scope_root)
    plans = _preflight_scope(scope, known_ids, durable_by_thread or {})
    result = MigrationResult()
    for plan in plans:
        result.merge(plan.result)
    if dry_run:
        return result
    for plan in plans:
        if plan.source_layout == _WORKING:
            _move_working_layout(plan)
        else:
            _move_structured_layout(plan)
    return result


def _known_thread_ids(thread_ids: Collection[str] | None) -> set[str]:
    """Require DB-known opaque IDs before inspecting any legacy directories."""
    if thread_ids is None:
        raise ValueError("thread_ids from the database are required")
    ids: set[str] = set()
    for thread_id in thread_ids:
        if not isinstance(thread_id, str):
            raise ValueError("thread_ids must contain strings")
        ids.add(thread_id)
    return ids


def _preflight_scope(
    scope: Path,
    thread_ids: set[str],
    durable_by_thread: dict[str, set[str]],
) -> list[_PlannedMove]:
    """Validate the entire migration selection and return immutable move plans."""
    plans: list[_PlannedMove] = []
    for thread_id in sorted(thread_ids):
        destination = resolve_thread_root(scope, thread_id)
        working_source = scope / _WORKING / thread_id
        structured_source = scope / "threads" / thread_id
        working_exists = _legacy_directory(working_source, "working")
        structured_exists = _legacy_directory(structured_source, "threads")

        if destination.exists():
            _reject_symlinks(destination, "destination")
            if working_exists or structured_exists:
                raise ValueError(
                    f"migration collision for {thread_id}: destination and legacy source both exist"
                )
            continue
        if working_exists and structured_exists:
            raise ValueError(
                f"migration collision for {thread_id}: both legacy working and threads sources exist"
            )
        if working_exists:
            durable = frozenset(durable_by_thread.get(thread_id, set()))
            plans.append(
                _PlannedMove(
                    source=working_source,
                    destination=destination,
                    source_layout=_WORKING,
                    durable_paths=durable,
                    result=_count_working_move(thread_id, working_source, durable),
                )
            )
        elif structured_exists:
            plans.append(
                _PlannedMove(
                    source=structured_source,
                    destination=destination,
                    source_layout="threads",
                    durable_paths=frozenset(),
                    result=_count_structured_move(thread_id, structured_source),
                )
            )
    return plans


def _legacy_directory(path: Path, layout_name: str) -> bool:
    """Return whether one legacy source exists after rejecting unsafe shapes."""
    parent = path.parent
    if parent.is_symlink() and (path.exists() or path.is_symlink()):
        raise ValueError(f"legacy {layout_name} source parent is a symlink: {parent}")
    if path.is_symlink():
        raise ValueError(f"legacy {layout_name} source is a symlink: {path}")
    if not path.exists():
        return False
    if not path.is_dir():
        raise ValueError(f"legacy {layout_name} source is not a directory: {path}")
    _reject_symlinks(path, f"legacy {layout_name} source")
    return True


def _reject_symlinks(root: Path, label: str) -> None:
    """Reject an existing migration source or target containing any symlink."""
    if root.is_symlink():
        raise ValueError(f"{label} is a symlink: {root}")
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"{label} contains a symlink: {path}")


def _count_working_move(
    thread_id: str,
    source: Path,
    durable_paths: Collection[str],
) -> MigrationResult:
    """Count a routed legacy working directory without touching the filesystem."""
    result = MigrationResult(threads_migrated=[thread_id])
    for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
        destination = _thread_dest_relpath(
            file_path.relative_to(source).as_posix(), durable_paths
        )
        _increment_counter(result, destination)
    return result


def _count_structured_move(thread_id: str, source: Path) -> MigrationResult:
    """Count an already-structured legacy thread directory without reformatting it."""
    result = MigrationResult(threads_migrated=[thread_id])
    for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = file_path.relative_to(source).as_posix()
        _increment_counter(result, relative)
    return result


def _increment_counter(result: MigrationResult, relative: str) -> None:
    """Classify a migrated file for the dormant script's dry-run summary."""
    first = relative.split("/", 1)[0]
    if first == _SKILLS:
        result.moved_skills += 1
    elif first == _CONTEXT:
        result.moved_context += 1
    else:
        result.moved_working += 1


def _move_working_layout(plan: _PlannedMove) -> None:
    """Move a preflighted ``working/<id>`` source directly into ``.threads``."""
    plan.destination.mkdir(parents=True, exist_ok=False)
    for directory in sorted(
        (path for path in plan.source.rglob("*") if path.is_dir()),
        key=lambda path: (len(path.parts), str(path)),
    ):
        if any(directory.iterdir()):
            continue
        rel = directory.relative_to(plan.source).as_posix()
        target = plan.destination / _thread_dest_relpath(rel, ())
        target.mkdir(parents=True, exist_ok=False)
    for source_file in sorted(
        path for path in plan.source.rglob("*") if path.is_file()
    ):
        rel = source_file.relative_to(plan.source).as_posix()
        target = plan.destination / _thread_dest_relpath(rel, plan.durable_paths)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_file), str(target))
    shutil.rmtree(plan.source)


def _move_structured_layout(plan: _PlannedMove) -> None:
    """Move a preflighted ``threads/<id>`` tree directly to ``.threads/<id>``."""
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan.source), str(plan.destination))


def _repoint_entry(entry: str, sid: str) -> str:
    """Rewrite only this session's legacy bootloader paths into ``.threads``."""
    working = _legacy_entry_match(entry, _WORKING, sid)
    if working is not None:
        prefix, tail = working
        destination = _thread_dest_relpath(tail, {tail})
        return f"{prefix}.threads/{sid}/{destination}"
    structured = _legacy_entry_match(entry, "threads", sid)
    if structured is not None:
        prefix, tail = structured
        return f"{prefix}.threads/{sid}/{tail}"
    return entry


def _legacy_entry_match(entry: str, layout: str, sid: str) -> tuple[str, str] | None:
    """Return the scope prefix and tail for one exact legacy path segment."""
    match = re.search(rf"(^|/){re.escape(layout)}/{re.escape(sid)}/", entry)
    if not match:
        return None
    prefix = entry[: match.start()] + match.group(1)
    return prefix, entry[match.end() :]


def session_working_relpaths(entries: list[str], sid: str) -> set[str]:
    """Return configured relpaths below this session's legacy working directory."""
    relpaths: set[str] = set()
    for entry in entries:
        match = _legacy_entry_match(str(entry), _WORKING, sid)
        if match is not None and match[1]:
            relpaths.add(match[1])
    return relpaths


def repoint_session_bootloader_paths(db, *, dry_run: bool = False) -> int:
    """Repoint only owning-session bootloader entries from legacy layouts.

    Legacy ``working/<own-id>`` entries route through the durable router,
    legacy ``threads/<own-id>`` entries receive a direct prefix replacement,
    and already canonical ``.threads`` entries remain unchanged.
    """
    rows = db.execute(
        "SELECT id, bootloader_files FROM threads "
        "WHERE bootloader_files IS NOT NULL AND bootloader_files != ''"
    ).fetchall()
    rewritten = 0
    for row in rows:
        sid = row["id"] if hasattr(row, "keys") else row[0]
        raw = row["bootloader_files"] if hasattr(row, "keys") else row[1]
        try:
            entries = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        original = [str(entry) for entry in entries]
        updated = [_repoint_entry(entry, str(sid)) for entry in original]
        if updated == original:
            continue
        rewritten += 1
        if not dry_run:
            db.execute(
                "UPDATE threads SET bootloader_files = ? WHERE id = ?",
                (json.dumps(updated), sid),
            )
    return rewritten
