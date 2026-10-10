"""Schema 14 -> 15: reset Codex thread handles so every Codex thread restarts cold.

Codex keeps the dynamic tools a provider thread was started with in that
thread's rollout and restores them on ``thread/resume``, which cannot replace
them. After a CORSIA tool is renamed, every resumed Codex thread still offers
the old name, which CORSIA no longer executes, and cannot call the new one.

Archiving each active ``codex``/``thread`` handle once, exactly as
``registry.models.retire`` archives one, sends each thread's next Turn down the
existing cold path: ``thread/start`` with the current dynamic tools and a replay
of CORSIA's own history. There is no compatibility alias and no second path.

The SQL is copied rather than imported so this step keeps producing the shape
it was written for. Launch metadata, including the rollout path, stays on the
archived identity; owner registrations keep their state, as ``retire`` leaves
them. The cost is one uncached re-prefill per Codex thread and the provider's
own reasoning continuity; restoring the runner's pre-migration backup restores
the handles.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sqlite3

log = logging.getLogger(__name__)

_ACTIVE_CODEX_THREAD_IDS = (
    "SELECT id FROM artifact_identities "
    "WHERE integration='codex' AND type='thread' AND state='active'"
)


def migrate_14_to_15(conn: sqlite3.Connection) -> None:
    """Archive every active Codex thread handle, as ``registry.models.retire`` does."""
    migrated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"""UPDATE artifact_registrations
            SET relationship_state='subscriber-archived', updated_at=?
            WHERE artifact_id IN ({_ACTIVE_CODEX_THREAD_IDS})
              AND relationship_state NOT IN
                  ('owner-pinned', 'owner-active', 'subscriber-parent-chain')""",
        (migrated_at,),
    )
    archived = conn.execute(
        """UPDATE artifact_identities SET state='archived', updated_at=?
           WHERE integration='codex' AND type='thread' AND state='active'""",
        (migrated_at,),
    ).rowcount
    log.info(
        "Archived %d Codex thread handle(s); each Codex thread's next Turn starts "
        "a fresh provider thread with the current tools",
        archived,
    )
