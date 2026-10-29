"""Project packaged System resources into the selected workspace.

The package remains the immutable source.  This module is the only writer of
``<workspace>/.corsia`` and makes its declared files readable to protected
agents without granting access to the install directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from core.system_projection_cache import (
    ProjectionState,
    Signature,
    capture_projection_state,
    fast_path_is_verified,
    manifest_only_signature,
    source_signature,
)
from core.workspace_layout import resolve_workspace_root
from infra.context_preview_timing import record_projection_lock_wait
from infra.system_package import (
    COORDINATION_MODELS_ROOT,
    system_package_dir,
    system_resource_source,
)


log = logging.getLogger(__name__)
_PROJECTION_DIR = ".corsia"
_MANIFEST_NAME = "MANIFEST.json"
_README_NAME = "README.md"
_GITIGNORE_NAME = ".gitignore"
_CONTENT_ROOTS = ("skills", "context", COORDINATION_MODELS_ROOT)
_GENERATED_GITIGNORE = (
    b"skills/\ncontext/\ncoordination-models/\nMANIFEST.json\nREADME.md\n.gitignore\n"
)
_GENERATED_README = (
    "# CORSIA System tier\n\n"
    "This content is generated from the installed CORSIA System package. It is "
    "rewritten whenever it drifts; do not edit it by hand. Choose different "
    "behavior through CORSIA options or a fork.\n"
).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Resource:
    path: PurePosixPath
    sha256: str


_CACHE: dict[Path, ProjectionState] = {}


def is_system_projection_path(path: Path, workspace: Path) -> bool:
    """Whether a file is one the last verified projection generated, not any ``.corsia`` file."""
    root = resolve_workspace_root(workspace)
    try:
        relative = PurePosixPath(path.expanduser().resolve().relative_to((root / _PROJECTION_DIR).resolve()).as_posix())
    except (OSError, ValueError):
        return False
    return root in _CACHE and any(item.path == relative for item in _CACHE[root].resources)


def ensure_system_projection(workspace: Path) -> Path | None:
    """Return the verified workspace projection, or ``None`` on any failure."""
    try:
        workspace_root = resolve_workspace_root(workspace)
        if not _agent_readable(workspace_root / _PROJECTION_DIR):
            log.warning("System projection unavailable: %s/%s is not ordinary agent read scope",
                        workspace_root, _PROJECTION_DIR)
            return None
        projection = workspace_root / _PROJECTION_DIR
        state = _CACHE.get(workspace_root)
        if state and fast_path_is_verified(projection, state):
            return projection
        with _workspace_lock(workspace_root):
            if not _projection_is_owned_or_empty(projection):
                raise ValueError("existing .corsia projection is not CORSIA-owned")
            changed: set[PurePosixPath] | None = None
            if state and manifest_only_signature(source_signature(())) == manifest_only_signature(
                state.source_signature
            ):
                resources, declared = state.resources, state.declared
                current_source_signature = source_signature(declared)
                if current_source_signature == state.source_signature:
                    changed = _changed_paths(projection, resources)
                    if not changed and not _has_undeclared(projection, set(resources)):
                        _CACHE[workspace_root] = _cache_state(
                            projection, resources, declared, current_source_signature,
                        )
                        return projection
                else:
                    resources, declared, current_source_signature = _load_resources()
            else:
                resources, declared, current_source_signature = _load_resources()
            _materialize(projection, resources, changed)
            _CACHE[workspace_root] = _cache_state(
                projection, resources, declared, current_source_signature,
            )
            return projection
    except Exception as exc:  # Projection failure must not advertise an unreadable path.
        log.warning("System projection unavailable: %s", exc)
        return None


def _agent_readable(projection: Path) -> bool:
    """Whether a protected Turn reads ``projection`` with no lease.

    A workspace root under the state root or a private root makes the
    projection lease-only or hard-floor; advertising it would hand agents an
    unreadable path.
    """
    from tools.host_path_policy import ordinary_host_path_denial
    from tools.sandbox_config import is_path_allowed

    return (ordinary_host_path_denial(projection, for_write=False) is None
            and is_path_allowed(projection))


def _workspace_lock(workspace_root: Path):
    """Lock one workspace directory across processes without leaving lock files.

    The blocked interval is recorded when this lock runs inside a timed preview.
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(workspace_root, os.O_RDONLY)

    class _Lock:
        def __enter__(self):
            started_at = time.perf_counter()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            record_projection_lock_wait(time.perf_counter() - started_at)
            return self

        def __exit__(self, *_args):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    return _Lock()


def _cache_state(projection, resources, declared, signature) -> ProjectionState:
    return capture_projection_state(
        projection, resources, declared, signature,
        (PurePosixPath(_MANIFEST_NAME), PurePosixPath(_README_NAME), PurePosixPath(_GITIGNORE_NAME)),
        _CONTENT_ROOTS,
    )


def _load_resources() -> tuple[tuple[_Resource, ...], tuple[PurePosixPath, ...], Signature]:
    """Read and validate manifest entries and their source bytes.

    A missing or drifted coordination model drops only that optional root, so
    System skills and context stay projected; any other failure is fatal.
    """
    manifest_raw = (system_package_dir() / _MANIFEST_NAME).read_bytes()
    entries = _manifest_entries(json.loads(manifest_raw.decode("utf-8")))
    resources: list[_Resource] = []
    unusable: list[str] = []
    for path, sha256 in entries:
        problem = _source_problem(path, sha256)
        if problem and path.parts[0] == COORDINATION_MODELS_ROOT:
            unusable.append(problem)
        elif problem:
            raise ValueError(problem)
        resources.append(_Resource(path, sha256))
    if unusable:
        log.warning("Coordination models not projected; regenerate the System manifest: %s",
                    "; ".join(unusable))
        resources = [item for item in resources if item.path.parts[0] != COORDINATION_MODELS_ROOT]
    declared = tuple(sorted(path for path, _sha256 in entries))
    return tuple(resources), declared, source_signature(declared)


def _manifest_entries(manifest: object) -> list[tuple[PurePosixPath, str]]:
    """Validate a manifest's file entries as unique (path, sha256) pairs."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise ValueError("invalid System manifest")
    entries: dict[PurePosixPath, str] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid System manifest entry")
        path = _resource_path(entry.get("path"))
        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"invalid System digest for {path}")
        if path in entries:
            raise ValueError(f"duplicate System resource {path}")
        entries[path] = sha256
    return list(entries.items())


def _source_problem(path: PurePosixPath, sha256: str) -> str | None:
    """Return why one declared source cannot be projected, or ``None``."""
    try:
        raw = system_resource_source(path).read_bytes()
    except FileNotFoundError:
        return f"System source missing for {path}"
    if hashlib.sha256(raw).hexdigest() != sha256:
        return f"System source digest mismatch for {path}"
    return None


def _resource_path(value: object) -> PurePosixPath:
    """Validate a manifest resource is one declared System content file."""
    if not isinstance(value, str):
        raise ValueError("System resource path must be a string")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or not path.parts
            or path.parts[0] not in _CONTENT_ROOTS):
        raise ValueError(f"invalid System resource path {value!r}")
    return path


def _changed_paths(projection: Path, resources: tuple[_Resource, ...]) -> set[PurePosixPath]:
    """Digest every projected file before accepting cached source verification."""
    changed = {
        item.path
        for item in resources
        if not _matches_digest(projection / Path(*item.path.parts), item.sha256)
    }
    generated = {
        PurePosixPath(_MANIFEST_NAME): (system_package_dir() / _MANIFEST_NAME).read_bytes(),
        PurePosixPath(_README_NAME): _GENERATED_README,
        PurePosixPath(_GITIGNORE_NAME): _GENERATED_GITIGNORE,
    }
    changed.update(
        path
        for path, raw in generated.items()
        if not _matches_bytes(projection / Path(*path.parts), raw)
    )
    return changed


def _matches_digest(target: Path, expected_sha256: str) -> bool:
    """Return whether one read-only target still has its declared bytes."""
    try:
        return (
            target.stat().st_mode & 0o777 == 0o444
            and hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha256
        )
    except FileNotFoundError:
        return False


def _matches_bytes(target: Path, expected: bytes) -> bool:
    """Return whether one generated target still has exact bytes and mode."""
    try:
        return target.stat().st_mode & 0o777 == 0o444 and target.read_bytes() == expected
    except FileNotFoundError:
        return False


def _projection_is_owned_or_empty(projection: Path) -> bool:
    """Refuse to claim occupied generated surfaces without a prior manifest."""
    claimed = (
        *(projection / root for root in _CONTENT_ROOTS),
        projection / _MANIFEST_NAME,
        projection / _README_NAME,
        projection / _GITIGNORE_NAME,
    )
    if not any(path.exists() or path.is_symlink() for path in claimed):
        return True
    try:
        manifest = json.loads((projection / _MANIFEST_NAME).read_text(encoding="utf-8"))
        return (
            isinstance(manifest.get("version"), str) and bool(_manifest_entries(manifest))
        )
    except (FileNotFoundError, ValueError, AttributeError):
        return False


def _materialize(
    projection: Path, resources: tuple[_Resource, ...], changed: set[PurePosixPath] | None
) -> None:
    """Synchronize the projection from the manifest-verified package bytes."""
    _ensure_directory(projection)
    package = system_package_dir()
    expected = {item.path for item in resources}
    for item in resources:
        if changed is not None and item.path not in changed:
            continue
        target = _target(projection, item.path)
        raw = system_resource_source(item.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item.sha256:
            raise ValueError(f"System source digest mismatch for {item.path}")
        _write_verified(target, raw, item.sha256)
    generated = {
        PurePosixPath(_MANIFEST_NAME): (package / _MANIFEST_NAME).read_bytes(),
        PurePosixPath(_README_NAME): _GENERATED_README,
        PurePosixPath(_GITIGNORE_NAME): _GENERATED_GITIGNORE,
    }
    for path, raw in generated.items():
        if changed is None or path in changed:
            _write_generated(projection / Path(*path.parts), raw)
    _remove_undeclared(projection, expected)


def _target(projection: Path, relative: PurePosixPath) -> Path:
    """Return a non-symlinked target contained by the projection root."""
    target = projection / Path(*relative.parts)
    if not target.is_relative_to(projection):
        raise ValueError(f"System projection escapes root: {relative}")
    _ensure_directory(target.parent)
    if target.is_symlink():
        raise ValueError(f"System projection target is a symlink: {relative}")
    return target


def _ensure_directory(directory: Path) -> None:
    """Create a projection directory only when no component is a symlink."""
    current = directory.anchor and Path(directory.anchor) or Path()
    for part in directory.parts[1:] if directory.is_absolute() else directory.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"System projection directory is a symlink: {current}")
    directory.mkdir(parents=True, exist_ok=True)


def _write_verified(target: Path, raw: bytes, expected_sha256: str) -> None:
    """Atomically restore one declared resource when its digest differs."""
    try:
        matches = hashlib.sha256(target.read_bytes()).hexdigest() == expected_sha256
    except FileNotFoundError:
        matches = False
    if not matches:
        _atomic_write(target, raw)
    target.chmod(0o444)


def _write_generated(target: Path, raw: bytes) -> None:
    """Atomically restore one generated support file when its bytes drift."""
    if not target.exists() or target.read_bytes() != raw:
        _atomic_write(target, raw)
    target.chmod(0o444)


def _atomic_write(target: Path, raw: bytes) -> None:
    """Write bytes beside their target, atomically replace it, and make it read-only."""
    with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        handle.write(raw)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, target)
        target.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)


def _non_read_only(projection: Path, expected: tuple[PurePosixPath, ...]) -> set[PurePosixPath]:
    """Return generated files whose permissions no longer match the projection."""
    return {
        path
        for path in expected
        if (projection / Path(*path.parts)).exists()
        and (projection / Path(*path.parts)).stat().st_mode & 0o777 != 0o444
    }


def _has_undeclared(projection: Path, resources: set[_Resource]) -> bool:
    """Return whether package-owned directories contain a stale projected file."""
    expected = {item.path for item in resources}
    return any(
        PurePosixPath(path.relative_to(projection).as_posix()) not in expected
        for section in _CONTENT_ROOTS
        for path in (projection / section).rglob("*")
        if path.is_symlink() or path.is_file()
    )


def _remove_undeclared(projection: Path, expected: set[PurePosixPath]) -> None:
    """Remove stale files under the package-owned content directories."""
    keep_directories: set[PurePosixPath] = set()
    for item in expected:
        parent = item.parent
        while parent != PurePosixPath("."):
            keep_directories.add(parent)
            parent = parent.parent
    for section in _CONTENT_ROOTS:
        root = projection / section
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            relative = PurePosixPath(path.relative_to(projection).as_posix())
            if path.is_symlink() or path.is_file():
                if relative not in expected:
                    path.unlink()
            elif path.is_dir() and relative not in keep_directories:
                path.rmdir()
        if PurePosixPath(section) not in keep_directories:
            root.rmdir()  # a dropped optional root leaves no empty directory
