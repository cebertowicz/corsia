"""Fail-silent product evidence for artifact registration origin outcomes."""

from __future__ import annotations

import json
import logging
import sqlite3

from infra.database import get_db
from infra.product_events import emit_product_event


log = logging.getLogger(__name__)


def emit_committed_origin(db, artifact_id: str, thread_id: str) -> None:
    """Emit the latest committed trusted-origin registration event."""
    connection = db if db is not None else get_db()
    try:
        row = connection.execute(
            """SELECT e.metadata,e.new_relationship_state,i.integration,i.type
                 FROM artifact_registration_events e
                 JOIN artifact_identities i ON i.id=e.artifact_id
                WHERE e.artifact_id=? AND e.affected_thread_id=?
                  AND e.event_type='registered'
                ORDER BY e.rowid DESC LIMIT 1""",
            (artifact_id, thread_id),
        ).fetchone()
        if row is None or not row["metadata"]:
            return
        metadata = json.loads(row["metadata"])
        if "origin_outcome" not in metadata:
            return
        emit_product_event(
            "artifact_origin",
            "recorded",
            thread_id=thread_id,
            properties={
                "outcome": metadata["origin_outcome"],
                "reason": metadata["origin_reason"],
                "producer": metadata["producer"],
                "node_state": metadata.get("node_state"),
                "usage_role": metadata.get("usage_role"),
                "registration": metadata["registration"],
                "relationship": row["new_relationship_state"],
                "artifact_type": f"{row['integration']}/{row['type']}",
                "contract_version": 1,
            },
        )
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        # Analytics is evidence only; durable registry truth is authoritative.
        log.warning(
            "Committed artifact origin telemetry failed: %s",
            type(exc).__name__,
        )
    finally:
        if db is None:
            connection.close()
