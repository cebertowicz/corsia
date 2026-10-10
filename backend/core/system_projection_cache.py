"""Metadata-only validation helpers for the System projection cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from infra.system_package import system_package_dir, system_resource_source


Signature = tuple[tuple[str, int, int], ...]
StatFingerprint = tuple[int, int, int, int, int]
PathFingerprints = tuple[tuple[PurePosixPath, StatFingerprint], ...]
_MISSING_FINGERPRINT = (-1, -1, -1, -1, -1)


@dataclass(frozen=True, slots=True)
class ProjectionState:
    """The existing projection cache plus its unlocked verification proof."""

    resources: tuple[Any, ...]
    declared: tuple[PurePosixPath, ...]
    source_signature: Signature
    projected_files: PathFingerprints
    content_directories: PathFingerprints


def capture_projection_state(
    projection: Path,
    resources: tuple[Any, ...],
    declared: tuple[PurePosixPath, ...],
    source_signature: Signature,
    generated_paths: tuple[PurePosixPath, ...],
    content_roots: tuple[str, ...],
) -> ProjectionState:
    """Capture the metadata proof used by the next unlocked projection read."""
    return ProjectionState(
        resources,
        declared,
        source_signature,
        _projected_file_fingerprints(projection, resources, generated_paths),
        _content_directory_fingerprints(projection, content_roots),
    )


def source_signature(declared: tuple[PurePosixPath, ...]) -> Signature:
    """Return stat signatures for the manifest and every declared source."""
    named = (
        ("MANIFEST.json", system_package_dir() / "MANIFEST.json"),
        *((str(path), system_resource_source(path)) for path in declared),
    )
    rows: list[tuple[str, int, int]] = []
    for name, path in named:
        try:
            stat = path.stat()
            rows.append((name, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            rows.append((name, -1, -1))
    return tuple(rows)


def manifest_only_signature(signature: Signature) -> Signature:
    """Return the manifest entry from a full source signature."""
    return tuple(item for item in signature if item[0] == "MANIFEST.json")


def fast_path_is_verified(projection: Path, state: ProjectionState) -> bool:
    """Return whether cached source and projected metadata still prove equivalence.

    This intentionally avoids both hashing and ``LOCK_EX``. A content directory's
    metadata is captured for every directory after materialization: a new or
    removed undeclared entry changes an already-captured parent's ctime/mtime,
    while a removed captured directory no longer has a matching fingerprint.
    Any uncertainty falls through to the locked digest verification and repair.
    """
    current_source_signature = source_signature(state.declared)
    return (
        manifest_only_signature(current_source_signature)
        == manifest_only_signature(state.source_signature)
        and current_source_signature == state.source_signature
        and _fingerprints_match(projection, state.projected_files)
        and _fingerprints_match(projection, state.content_directories)
    )


def _projected_file_fingerprints(
    projection: Path,
    resources: tuple[Any, ...],
    generated_paths: tuple[PurePosixPath, ...],
) -> PathFingerprints:
    """Fingerprint every declared and generated projection file without reading it."""
    paths = {item.path for item in resources}
    paths.update(generated_paths)
    fingerprints: list[tuple[PurePosixPath, StatFingerprint]] = []
    for path in sorted(paths):
        fingerprint = _stat_fingerprint(projection / Path(*path.parts))
        if fingerprint is None:
            raise ValueError(f"projected System file missing after materialization: {path}")
        fingerprints.append((path, fingerprint))
    return tuple(fingerprints)


def _content_directory_fingerprints(
    projection: Path, content_roots: tuple[str, ...],
) -> PathFingerprints:
    """Fingerprint every content directory, including optional missing roots."""
    paths = {PurePosixPath(section) for section in content_roots}
    for section in content_roots:
        root = projection / section
        if root.is_dir():
            paths.update(
                PurePosixPath(path.relative_to(projection).as_posix())
                for path in root.rglob("*")
                if path.is_dir()
            )
    return tuple(
        (path, _stat_fingerprint(projection / Path(*path.parts)) or _MISSING_FINGERPRINT)
        for path in sorted(paths)
    )


def _fingerprints_match(projection: Path, expected: PathFingerprints) -> bool:
    """Return whether each cached lstat fingerprint still matches exactly."""
    return all(
        (_stat_fingerprint(projection / Path(*path.parts)) or _MISSING_FINGERPRINT) == fingerprint
        for path, fingerprint in expected
    )


def _stat_fingerprint(path: Path) -> StatFingerprint | None:
    """Return a no-content-read fingerprint; lstat makes symlink swaps visible."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode
