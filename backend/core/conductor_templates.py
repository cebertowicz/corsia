"""Versioned conductor templates exposed through the canonical context ledger."""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Sequence

from core.effective_context import ContextSourceEntry
from infra.conductor import ConductorScope, get_conductor_scopes


_PACKAGE = "prompts.conductor"
_VERSION = "v2"
_RESOURCE_BY_KIND = {
    "core": "core-v2.md",
    "workspace": "workspace-v2.md",
    "project": "project-v2.md",
}


def _selected_template_kinds(scopes: Sequence[ConductorScope]) -> tuple[str, ...]:
    """Select each role template once in canonical delivery order."""
    if not scopes:
        return ()
    scope_types = {scope.get("scope_type") for scope in scopes}
    selected = ["core"]
    if "workspace" in scope_types:
        selected.append("workspace")
    if "project" in scope_types:
        selected.append("project")
    return tuple(selected)


def _load_template(kind: str, scopes: Sequence[ConductorScope]) -> ContextSourceEntry:
    """Load and strictly decode one packaged template at collection time."""
    resource = _RESOURCE_BY_KIND[kind]
    raw = resources.files(_PACKAGE).joinpath(resource).read_bytes()
    content = raw.decode("utf-8", errors="strict")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    return ContextSourceEntry(
        source_id=f"conductor-template:{kind}:{_VERSION}",
        origin_section="conductor",
        label=f"Conductor {kind} template ({_VERSION})",
        stable_reference=f"package://{_PACKAGE}/{resource}",
        freshness="packaged",
        inclusion="included",
        reason=f"designated {kind} conductor context",
        owner_surface="conductor-designation",
        prompt_content=content,
        metadata={
            "provider": "conductor_template",
            "template_kind": kind,
            "template_version": _VERSION,
            "package": _PACKAGE,
            "resource": resource,
            "raw_sha256": raw_sha256,
            "raw_byte_count": len(raw),
            "conductor_scopes": [dict(scope) for scope in scopes],
            "repair_owner": "conductor-designation",
        },
    )


def conductor_template_sources(
    thread_id: str,
    *,
    db=None,
    scopes: Sequence[ConductorScope] | None = None,
) -> list[ContextSourceEntry]:
    """Return packaged conductor templates selected by current designation."""
    resolved_scopes = (
        list(scopes)
        if scopes is not None
        else get_conductor_scopes(thread_id, db=db)
    )
    return [
        _load_template(kind, resolved_scopes)
        for kind in _selected_template_kinds(resolved_scopes)
    ]
