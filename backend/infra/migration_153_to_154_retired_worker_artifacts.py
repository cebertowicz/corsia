"""Schema 153 -> 154: give back artifacts the old settled-worker hand-over released.

Retiring a settled worker used to release every artifact it owned, naming its
parent as triage, before archiving it; the re-home on thread end then found
nothing to move and the artifacts stayed ownerless. Each such artifact whose
latest disposition is still that release goes back to the worker, and every
ended worker is re-homed by the registry's own thread-end rules, so old and new
retirements end in one shape. Runs inside the startup migration transaction.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def migrate_153_to_154(conn: sqlite3.Connection) -> None:
    from registry.identity_bridge import OWNER_REGISTRATION_STATES
    from registry.ownership import rehome_artifact, rehome_on_thread_end

    needed = ("artifact_registration_events", "artifact_identities", "artifact_registrations", "threads")
    present = {row[0] for row in conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' for _ in needed)})",
        needed,
    )}
    if present != set(needed):
        return
    # No event_type/source_path index exists. Materialize matching releases in
    # one pass; the correlated latest-event lookup uses the artifact index.
    conn.execute("""CREATE TEMP TABLE retired_worker_releases AS
        SELECT rowid AS event_rowid, artifact_id, old_owner_thread_id
        FROM artifact_registration_events
        WHERE event_type='ownership_released' AND source_path='plans.worker_retirement'
    """)
    marks = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = conn.execute(f"""
        SELECT i.integration, i.type, i.external_id, t.id AS worker, t.lifecycle_state
        FROM retired_worker_releases e
        JOIN artifact_identities i ON i.id=e.artifact_id AND i.state='active'
        JOIN threads t ON t.id=e.old_owner_thread_id
        WHERE NOT EXISTS (
            SELECT 1 FROM artifact_registrations r
            WHERE r.artifact_id=i.id AND r.relationship_state IN ({marks})
        ) AND e.event_rowid = (
            SELECT latest.rowid FROM artifact_registration_events latest
            WHERE latest.artifact_id=i.id
              AND latest.event_type IN ('ownership_released','ownership_rehomed')
            ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1
        )
    """, OWNER_REGISTRATION_STATES).fetchall()
    conn.execute("DROP TABLE retired_worker_releases")
    ended_workers = {}
    for row in rows:
        rehome_artifact(
            integration=row["integration"], type=row["type"], external_id=row["external_id"],
            new_owner_thread_id=row["worker"], actor_thread_id=None, authority="migration",
            reason="restore owner released by retired-worker hand-over",
            source_path="infra.migration_153_to_154", show_to_new_owner=False, db=conn,
        )
        if row["lifecycle_state"] in ("archived", "purged"):
            ended_workers[row["worker"]] = row["lifecycle_state"]
    for worker, state in ended_workers.items():
        rehome_on_thread_end(conn, worker, state)
    logger.info("Restored %d retired-worker artifacts", len(rows))
