"""What ``types`` shows: each connector's declared event types and fields, and the artifact types it
reads from a tool result with the shipped command rule that registers each. Names no connector."""

from __future__ import annotations

from typing import Any

from events import connectors
from events.rule_actions import RuleRefused


def _artifact_types(manifest: connectors.ConnectorManifest) -> list[dict[str, Any]]:
    """Every artifact type the connector's events or extractors name; ``extractor`` says whether an
    ``observe`` rule can register it from a tool result (and ``shipped_commands`` which do already)."""
    names = sorted({decl.artifact_type for decl in manifest.event_types.values() if decl.artifact_type}
                   | set(manifest.artifact_extractors))
    return [
        {"item_type": name, "extractor": name in manifest.artifact_extractors,
         "shipped_commands": [
             {"tool": trigger.tool_name, "argv_prefix": list(trigger.argv_prefix)}
             for trigger in manifest.command_triggers if trigger.item_type == name]}
        for name in names
    ]


def types(connector: Any = None) -> dict[str, Any]:
    """The declared types of one connector, or of every connector."""
    if connector is not None and connector not in connectors.registered_connectors():
        raise RuleRefused(f"no connector {connector!r}; the connectors are {list(connectors.registered_connectors())}")
    return {"connectors": [
        {"connector": name,
         "event_types": [{"event_type": decl.event_type, "filterable_fields": list(decl.filterable_fields)}
                         for decl in manifest.event_types.values()],
         "artifact_types": _artifact_types(manifest)}
        for name in connectors.registered_connectors()
        if connector in (None, name) for manifest in (connectors.get_manifest(name),)
    ]}
