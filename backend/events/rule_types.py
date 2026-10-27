"""What ``types`` shows: each connector's declared event types and fields, and the artifact types it
reads from a tool result with the shipped command rule that registers each. The shipped commands are
read from the shipped rows themselves, the rows the observer applies. Names no connector."""

from __future__ import annotations

import sqlite3
from typing import Any

from events import connectors, observe_rules
from events.rule_actions import RuleRefused


def _shipped_commands(db: sqlite3.Connection) -> list[observe_rules.ObserveRule]:
    return [observe_rules.from_row(row) for row in db.execute(
        "SELECT * FROM event_rules WHERE kind='observe' AND scope=? AND enabled=1 "
        "ORDER BY created_at, id", (observe_rules.SHIPPED_SCOPE,)).fetchall()]


def _artifact_types(
    manifest: connectors.ConnectorManifest, shipped: list[observe_rules.ObserveRule],
) -> list[dict[str, Any]]:
    """Every artifact type the connector's events or extractors name; ``extractor`` says whether an
    ``observe`` rule can register it from a tool result (and ``shipped_commands`` which do already)."""
    names = sorted({decl.artifact_type for decl in manifest.event_types.values() if decl.artifact_type}
                   | set(manifest.artifact_extractors))
    return [
        {"item_type": name, "extractor": name in manifest.artifact_extractors,
         "shipped_commands": [
             {"tool": rule.tool, "argv_prefix": list(rule.argv_prefix)}
             for rule in shipped if rule.connector == manifest.connector and rule.item_type == name]}
        for name in names
    ]


def types(db: sqlite3.Connection, connector: Any = None) -> dict[str, Any]:
    """The declared types of one connector, or of every connector."""
    if connector is not None and connector not in connectors.registered_connectors():
        raise RuleRefused(f"no connector {connector!r}; the connectors are {list(connectors.registered_connectors())}")
    shipped = _shipped_commands(db)
    return {"connectors": [
        {"connector": name,
         "event_types": [{"event_type": decl.event_type, "filterable_fields": list(decl.filterable_fields)}
                         for decl in manifest.event_types.values()],
         "artifact_types": _artifact_types(manifest, shipped)}
        for name in connectors.registered_connectors()
        if connector in (None, name) for manifest in (connectors.get_manifest(name),)
    ]}
