"""Schema step 8 -> 9: tombstone obsolete generated plan controls.

Schema 8 archived the generic blocked-plan controls, but archived identities
remain visible in Artifact pane History. Structured Plans is now the canonical
blocked-work projection, so this data-only step tombstones only local/plan-node
attention identities carrying the generated control's exact metadata fingerprint.

The startup migration runner owns the transaction, backup, version update, and
validation. The producer metadata plus its matching attention link distinguish
generated controls from user-created plan-node lookalikes, which this step
preserves along with decisions, non-attention plan links, metadata, and owner
registrations.
"""

from __future__ import annotations

import sqlite3


def migrate_8_to_9(db: sqlite3.Connection) -> None:
    """Tombstone exact legacy generated plan controls and passive registrations."""
    migrated_at = str(
        db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    )
    target = """
        SELECT i.id
        FROM artifact_identities i
        WHERE i.integration='local'
          AND i.type='plan-node'
          AND CASE
                WHEN json_valid(i.metadata)
                THEN json_extract(i.metadata,'$.plan_node_id')=i.external_id
                ELSE 0
              END
          AND i.state IN ('active','archived')
          AND EXISTS (
              SELECT 1
              FROM plan_node_artifact_links l
              WHERE l.artifact_id=i.id
                AND l.role='attention'
                AND l.node_id=i.external_id
          )
    """
    # Resolve registrations before tombstoning identities: ``target`` deliberately
    # excludes tombstones, so reversing these statements would skip registration
    # cleanup.
    db.execute(
        f"""UPDATE artifact_registrations
            SET relationship_state='tombstoned',updated_at=?
            WHERE artifact_id IN ({target})
              AND relationship_state NOT IN
                  ('owner-pinned','owner-active','subscriber-parent-chain')""",
        (migrated_at,),
    )
    db.execute(
        f"""UPDATE artifact_identities
            SET state='tombstone',updated_at=?
            WHERE id IN ({target})""",
        (migrated_at,),
    )
