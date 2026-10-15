"""Versioned conductor and CORSIA agent templates for the canonical context ledger."""

from __future__ import annotations

import hashlib
from importlib import resources
from typing import Sequence

from core.effective_context import ContextSourceEntry
from infra.conductor import CoordinationScope, get_coordination_scopes


_PACKAGE = "prompts.conductor"
_VERSION = "v2"
_RESOURCE_BY_KIND = {
    "core": "core-v2.md",
    "project": "project-v2.md",
    "corsia_agent": "corsia-agent-v2.md",
}


def _selected_template_kinds(scopes: Sequence[CoordinationScope]) -> tuple[str, ...]:
    """Select each role template once in canonical delivery order.

    Both coordinating roles receive the shared contract; project and workspace
    scope templates then add only their role-specific responsibilities.
    """
    scope_types = {scope.get("scope_type") for scope in scopes}
    selected: list[str] = []
    if scope_types:
        selected.append("core")
    if "project" in scope_types:
        selected.append("project")
    if "workspace" in scope_types:
        selected.append("corsia_agent")
    return tuple(selected)


def _load_template(kind: str, scopes: Sequence[CoordinationScope]) -> ContextSourceEntry:
    """Load and strictly decode one packaged template at collection time."""
    resource = _RESOURCE_BY_KIND[kind]
    raw = resources.files(_PACKAGE).joinpath(resource).read_bytes()
    content = raw.decode("utf-8", errors="strict")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    is_agent = kind == "corsia_agent"
    return ContextSourceEntry(
        source_id=(
            f"corsia-agent-template:{_VERSION}"
            if is_agent
            else f"conductor-template:{kind}:{_VERSION}"
        ),
        origin_section="conductor",
        label=(
            f"CORSIA agent template ({_VERSION})"
            if is_agent
            else f"Conductor {kind} template ({_VERSION})"
        ),
        stable_reference=f"package://{_PACKAGE}/{resource}",
        freshness="packaged",
        inclusion="included",
        reason=(
            "designated CORSIA agent context"
            if is_agent
            else f"designated {kind} conductor context"
        ),
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
    scopes: Sequence[CoordinationScope] | None = None,
) -> list[ContextSourceEntry]:
    """Return packaged templates selected by current designation."""
    resolved_scopes = (
        list(scopes)
        if scopes is not None
        else get_coordination_scopes(thread_id, db=db)
    )
    return [
        _load_template(kind, resolved_scopes)
        for kind in _selected_template_kinds(resolved_scopes)
    ]
