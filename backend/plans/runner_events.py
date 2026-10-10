"""Append-only plan audit events owned by continuation runners."""

from __future__ import annotations

import json


def record(
    node_id: str,
    event: str,
    *,
    provenance: str,
    payload: dict,
    db=None,
) -> bool:
    """Append one idempotent runner audit event without changing node state."""

    from plans import service

    normalized_event = service._required_text(event, "runner event")
    normalized_provenance = service._required_text(provenance, "provenance")
    payload_json = json.dumps(payload, sort_keys=True)
    with service._connection(db) as connection, service._write(connection):
        service._node_row(connection, node_id)
        existing = connection.execute(
            """SELECT 1 FROM plan_node_events
               WHERE node_id=? AND event=? AND provenance=?
                 AND payload_json=? LIMIT 1""",
            (node_id, normalized_event, normalized_provenance, payload_json),
        ).fetchone()
        if existing is not None:
            return False
        service._event(
            connection,
            node_id,
            normalized_event,
            actor_id=None,
            provenance=normalized_provenance,
            payload=payload,
        )
        return True
