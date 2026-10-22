"""What a rule may name on a type an agent declared: the type and its matchable fields.

A declared type's fields are its declared attributes. A ``record`` type is never
routed, so no rule can fire on it; a ``num`` attribute is a measurement, so no
rule matches on it. Both are refused when the rule is written. The retained intake
refusals of a type, which explain a rule that never fired, are read here too.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from events import rules
from events.connectors import EventTypeDecl, registered_connectors
from infra.measure_registry import REGISTRY, DeclaredEvent

UNMATCHABLE_KIND = "num"


def declared_event(db: sqlite3.Connection, connector: str, event_type: str) -> tuple[EventTypeDecl, DeclaredEvent] | None:
    """The declaration of ``connector.event_type`` when an agent declared it, else ``None``.

    The one place a declared type becomes an ``EventTypeDecl``: rules match on its fields and
    intake delivers by it (``intake.resolve_event_type``). A connector's namespace, and ``worker``,
    is never an agent's, so a row synced under one can't shadow its manifest.
    """
    if connector == "worker" or connector in registered_connectors():
        return None
    entry = REGISTRY.ensure_loaded(db).events.get(f"{connector}.{event_type}")
    if entry is None:
        return None
    return EventTypeDecl(event_type, (), tuple(entry.declared)), entry


def match_problem(entry: DeclaredEvent, event_type: str, tests: list[Mapping[str, Any]]) -> str | None:
    """Why a rule on this declared type is refused, or ``None``."""
    if entry.use == "record":
        return (f"{event_type} is declared use: record, so it is never routed and no rule can fire on it; "
                "its owner declares use: route or both to make it routable.")
    for test in tests:
        kind = entry.specs[test["field"]].kind
        if kind == UNMATCHABLE_KIND:
            return (f"{test['field']} is a {kind} attribute, a measurement; a rule can't match on it. "
                    "Match an id, count, enum or login attribute instead.")
    return None


def intake_refusals(db: sqlite3.Connection, *, project_id: str | None, event_type: str, limit: int = 50) -> list[dict[str, Any]]:
    """Recent refusals of ``event_type`` from this project's agents, newest first; never the facts."""
    if not project_id:
        return []
    since = (datetime.now(timezone.utc) - rules.RETENTION).isoformat()
    rows = db.execute(
        "SELECT r.event_id, r.event_type, r.refusal_code, r.producer, r.origin, r.at "
        "FROM event_intake_records r JOIN threads t ON t.id=r.producer "
        "WHERE r.outcome='refused' AND r.event_type=? AND r.at>=? AND t.project_id=? "
        "ORDER BY r.id DESC LIMIT ?",
        (event_type, since, project_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]
