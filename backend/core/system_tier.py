"""Load the immutable packaged CORSIA System tier into context rows."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core.effective_context import ContextSourceEntry
from core.role_context_sources import RoleName
from infra.system_package import (
    SYSTEM_VERSION,
    system_package_ref,
    system_resource_source,
)


log = logging.getLogger(__name__)
_VERSION = SYSTEM_VERSION
_MANIFEST_RESOURCE = "MANIFEST.json"
_WARNED_MISMATCHES: set[tuple[str, str]] = set()
_WARNED_UNREADABLE: list[bool] = []


@dataclass(frozen=True, slots=True)
class SystemTierFile:
    """One declared packaged System resource."""

    path: str
    sha256: str
    byte_count: int
    kind: str
    role: RoleName | None


@dataclass(frozen=True, slots=True)
class SystemTierMismatch:
    """A manifest entry whose on-disk package resource differs from its digest."""

    path: str
    expected_sha256: str
    actual_sha256: str


@dataclass(frozen=True, slots=True)
class SystemTierManifest:
    """The packaged System version, declared resources, and detected drift."""

    version: str
    files: tuple[SystemTierFile, ...]
    mismatches: tuple[SystemTierMismatch, ...]


def _resource_path(relative_path: str):
    """Return one package resource after rejecting manifest path escapes."""
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"invalid System manifest resource path {relative_path!r}")
    return system_resource_source(path)


def _read(relative_path: str) -> tuple[bytes, str]:
    """Read and strictly decode one packaged System resource."""
    raw = _resource_path(relative_path).read_bytes()
    return raw, raw.decode("utf-8", errors="strict")


def _parse_file(item: Any) -> SystemTierFile:
    """Validate one manifest item before it can influence context delivery."""
    if not isinstance(item, dict):
        raise ValueError("System manifest files must contain objects")
    path = item.get("path")
    sha256 = item.get("sha256")
    byte_count = item.get("bytes")
    kind = item.get("kind")
    role = item.get("role")
    if not isinstance(path, str) or not isinstance(sha256, str):
        raise ValueError("System manifest entries require path and sha256 strings")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        raise ValueError(f"System manifest entry {path!r} has invalid bytes")
    # Coordination models are declared for projection and drift checks only;
    # system_tier_sources loads ``context`` rows, never them.
    if kind not in {"context", "skill", "coordination-model"}:
        raise ValueError(f"System manifest entry {path!r} has invalid kind {kind!r}")
    if role not in {None, "agent", "conductor", "corsia_agent"}:
        raise ValueError(f"System manifest entry {path!r} has invalid role {role!r}")
    _resource_path(path)
    return SystemTierFile(path, sha256, byte_count, kind, role)


def _manifest_files() -> tuple[SystemTierFile, ...]:
    """Read and validate the canonical manifest declaration."""
    _raw, text = _read(_MANIFEST_RESOURCE)
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        raise ValueError(f"System manifest must declare version {_VERSION!r}")
    items = payload.get("files")
    if not isinstance(items, list):
        raise ValueError("System manifest files must be a list")
    files = tuple(_parse_file(item) for item in items)
    if len({item.path for item in files}) != len(files):
        raise ValueError("System manifest contains duplicate resource paths")
    return files


def _mismatches(files: tuple[SystemTierFile, ...]) -> tuple[SystemTierMismatch, ...]:
    """Compare each declared resource digest to the bytes currently on disk."""
    found: list[SystemTierMismatch] = []
    for item in files:
        try:
            raw, _content = _read(item.path)
        except FileNotFoundError:
            actual_sha256 = "missing"
        else:
            actual_sha256 = hashlib.sha256(raw).hexdigest()
        if actual_sha256 != item.sha256:
            mismatch = SystemTierMismatch(item.path, item.sha256, actual_sha256)
            found.append(mismatch)
            warning_key = (item.path, actual_sha256)
            if warning_key not in _WARNED_MISMATCHES:
                _WARNED_MISMATCHES.add(warning_key)
                log.warning(
                    "System manifest mismatch for %s: manifest sha256 %s, "
                    "on-disk sha256 %s; regenerate the manifest",
                    item.path,
                    item.sha256,
                    actual_sha256,
                )
    return tuple(found)


def system_skill_names() -> frozenset[str]:
    """Names of the packaged System skills, from the manifest.

    With no readable manifest the names are unknown: the caller then uses local
    copies, visibly behind its System-unavailable row, and this warns once.
    """
    try:
        files = system_tier_manifest().files
    except (OSError, ValueError) as exc:
        if not _WARNED_UNREADABLE:
            _WARNED_UNREADABLE.append(True)
            log.warning("System skill manifest unreadable; local copies are used: %s", exc)
        return frozenset()
    return frozenset(PurePosixPath(f.path).parts[1] for f in files if f.kind == "skill")


def system_tier_manifest() -> SystemTierManifest:
    """Return the System manifest and visible integrity state for its resources."""
    files = _manifest_files()
    return SystemTierManifest(_VERSION, files, _mismatches(files))


def _mismatch_error_row(mismatches: tuple[SystemTierMismatch, ...]) -> ContextSourceEntry:
    """Make package drift visible without dropping the bytes still on disk."""
    manifest_raw, _manifest_text = _read(_MANIFEST_RESOURCE)
    details = "; ".join(
        f"{item.path}: manifest sha256 {item.expected_sha256}, on-disk sha256 "
        f"{item.actual_sha256}"
        for item in mismatches
    )
    return ContextSourceEntry(
        source_id="corsia:error:manifest-mismatch",
        origin_section="corsia",
        label=f"System manifest integrity ({_VERSION})",
        stable_reference=system_package_ref(_MANIFEST_RESOURCE),
        freshness="error",
        inclusion="omitted",
        reason=f"System manifest mismatch for {details}; regenerate the manifest",
        owner_surface="system-tier",
        prompt_content=None,
        metadata={
            "provider": "system_tier",
            "system_version": _VERSION,
            "resource": _MANIFEST_RESOURCE,
            "raw_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "raw_byte_count": len(manifest_raw),
            "repair_owner": "corsia-release",
        },
    )


def system_tier_prompt(workspace: Path, installed: list[Any] | None) -> str:
    """Render System rows from an install boundary or the packaged tier."""
    if installed is not None:
        from core.installed_context import installed_parts

        return "\n\n".join(installed_parts(installed, "corsia"))
    return "\n\n".join(
        source.prompt_content or ""
        for source in system_tier_sources((), workspace=workspace)
        if source.inclusion in {"included", "summarized"}
    )


def system_tier_component_fields(content: str) -> dict[str, str]:
    """Return the Native System component fields keyed by dataclass field name."""
    return {
        "component_id": "corsia",
        "origin_section": "corsia",
        "label": "System (v1)",
        "stable_reference": system_package_ref(),
        "owner_surface": "system-tier",
        "content": content,
    }


def system_tier_components(workspace: Path, installed: list[Any] | None) -> list[Any]:
    """Return the Native System component, or none when there is no content."""
    from core.context_builder import SystemPromptComponent

    content = system_tier_prompt(workspace, installed)
    return [SystemPromptComponent(**system_tier_component_fields(content))] if content else []


def system_tier_sources(
    roles: tuple[RoleName, ...], *, workspace: Path
) -> list[ContextSourceEntry]:
    """Return applicable immutable System context rows in manifest order."""
    from core.system_projection import ensure_system_projection

    projection = ensure_system_projection(workspace)
    manifest = system_tier_manifest()
    rows: list[ContextSourceEntry] = []
    for item in manifest.files:
        if item.kind != "context" or (item.role and item.role not in roles):
            continue
        try:
            raw, content = _read(item.path)
        except FileNotFoundError:
            continue
        rows.append(
            ContextSourceEntry(
                source_id=f"corsia:{_VERSION}:{item.path}",
                origin_section="corsia",
                label=f"System: {PurePosixPath(item.path).name} ({_VERSION})",
                stable_reference=system_package_ref(item.path),
                freshness="packaged",
                inclusion="included",
                reason="ships with CORSIA" + (f"; {item.role} role" if item.role else ""),
                owner_surface="system-tier",
                prompt_content=f"## {PurePosixPath(item.path).name}\n\n{content}",
                metadata={
                    "provider": "system_tier",
                    "system_version": _VERSION,
                    "resource": item.path,
                    "path": (
                        str(projection / Path(*PurePosixPath(item.path).parts))
                        if projection
                        else None
                    ),
                    "role": item.role,
                    "raw_sha256": hashlib.sha256(raw).hexdigest(),
                    "raw_byte_count": len(raw),
                    "repair_owner": "corsia-release",
                },
            )
        )
    if manifest.mismatches:
        rows.append(_mismatch_error_row(manifest.mismatches))
    return rows
