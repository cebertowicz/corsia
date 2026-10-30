"""Connector-owned shipped workflows and their declared result contract."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.skill_registry_types import SkillRow

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ShippedResultContract:
    """Identity fields select the same-named claimed-event facts, never model output."""

    source: str
    kind: str
    item_fields: tuple[str, ...]
    version: Callable[[], str]
    instructions: Callable[[], str]
    capture: Callable[[Path], JsonObject]
    validate: Callable[[JsonObject, JsonObject], JsonObject]

    def context(self, origin: str, name: str, facts: Mapping[str, Any]) -> JsonObject:
        return {"origin": origin, "name": name, "source": self.source, "kind": self.kind,
                "item": {key: facts[key] for key in self.item_fields if key in facts},
                "staging_path": facts.get("staging_path"), "record": None}


@dataclass(frozen=True)
class ShippedWorkflowDecl:
    load: Callable[[str], SkillRow]
    result: ShippedResultContract


@dataclass(frozen=True)
class ShippedWorkflowResolution:
    declaration: ShippedWorkflowDecl | None
    error: str | None = None


def resolve_shipped_workflow(origin: str | None, name: str) -> ShippedWorkflowResolution | None:
    """None means a workspace workflow. A shipped origin can only resolve or refuse.

    A known declaration still supplies the refusal envelope on a stale package
    version; its loader is never called until the version check succeeds.
    """
    from events.connectors import get_manifest

    if not origin or not origin.startswith("rule_set:"):
        return None
    connector, separator, version = origin.removeprefix("rule_set:").partition("@")
    try:
        manifest = get_manifest(connector)
    except KeyError:
        return ShippedWorkflowResolution(None, "the shipped connector is no longer available")
    declaration = manifest.shipped_workflows.get(name)
    if not separator or manifest.version != version:
        return ShippedWorkflowResolution(declaration, "the shipped workflow package version is unavailable")
    if declaration is None:
        return ShippedWorkflowResolution(None, "the shipped workflow is no longer available")
    return ShippedWorkflowResolution(declaration)


def shipped_item_fields() -> tuple[str, ...]:
    """Worker event fields contributed by connector-owned declarations."""
    from events.connectors import get_manifest, registered_connectors

    return tuple(sorted({name for connector in registered_connectors()
                         for workflow in get_manifest(connector).shipped_workflows.values()
                         for name in workflow.result.item_fields}))
