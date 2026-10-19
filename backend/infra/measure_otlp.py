"""Decode one OTLP/JSON log record into the arguments of the shared admission function.

The loopback receiver (a later slice) will call this per record; the key's agent and
project are stamped by the server, so the record's own ``corsia.*`` values are claims.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from infra.measure_admission import Admission, Claims, admit

_CLAIM_KEYS = {
    "corsia.agent_thread_id": "agent_thread_id",
    "corsia.project_id": "project_id",
    "corsia.run_id": "run_id",
    "corsia.workflow.ref": "workflow_ref",
    "corsia.workflow.version": "workflow_version",
    "corsia.subject": "subject",
}


def _value(raw: dict[str, Any]) -> Any:
    if "stringValue" in raw:
        return raw["stringValue"]
    if "intValue" in raw:
        return int(raw["intValue"])
    if "doubleValue" in raw:
        return raw["doubleValue"]
    return raw.get("boolValue")


def admit_otlp_record(
    db: sqlite3.Connection, record: dict[str, Any], *, agent_thread_id: str, project_id: str | None,
    provenance: str = "agent_recorded",
) -> Admission:
    """Admit one OTLP/JSON logRecord for the agent its key resolves to."""
    attributes = {item["key"]: _value(item.get("value", {})) for item in record.get("attributes", [])}
    event_name = attributes.pop("event.name", None)
    claims = Claims(**{field: attributes.pop(key) for key, field in _CLAIM_KEYS.items() if key in attributes})
    nanos = record.get("timeUnixNano")
    ts = datetime.fromtimestamp(int(nanos) / 1e9, tz=timezone.utc).isoformat() if nanos else None
    return admit(
        db, owner_thread_id=agent_thread_id, event_name=str(event_name or ""), attributes=attributes,
        source="otlp", provenance=provenance, project_id=project_id, claims=claims, ts=ts,
    )
