"""Lift a retirement once its artifact has become exempt from retirement.

An exemption (``registry.retirement_predicate``) only stops a later
retirement. A row retired before its artifact became exempt would stay hidden
from the owner, so the write that creates the exemption calls this to show it
again, through the rebind a deliberate hand-off uses. The same call shows an
owner row that an ownership change settled: ownership alone never shows a
row, but the user's comment is the owner's reason to see the document.
"""

from __future__ import annotations

from registry.change_notifications import build_artifact_change_messages
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    OriginRebind,
    _upsert_registration,
)
from registry.registration_events import _append_registration_events, _now
from registry.retirement_predicate import RETIREMENT_NOT_EXEMPT_CLAUSE

#: The registration event that records an exemption lifting a retirement.
EXEMPT_ROW_RESTORED_EVENT = "exempt_row_restored"


def restore_exempt_owner_row(db, *, artifact_id: str, trigger: str) -> int:
    """Show the owner its settled row again once its artifact has become exempt.

    A row the user removed stays removed, and a row that is not exempt is left
    alone. The owner's pane is nudged once the caller's write commits.
    """
    owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""SELECT r.id,r.parent_thread_id,r.relationship_state
             FROM artifact_registrations AS r
            WHERE r.artifact_id=? AND r.surfacing='settled'
              AND r.relationship_state IN ({owner_states})
              AND NOT ({RETIREMENT_NOT_EXEMPT_CLAUSE})""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    now = _now()
    for row in rows:
        _upsert_registration(
            db,
            artifact_id=artifact_id,
            parent_thread_id=row["parent_thread_id"],
            relationship_state=row["relationship_state"],
            now=now,
            rebind=OriginRebind(None),
        )
    _append_registration_events(
        db,
        (
            {
                "artifact_id": artifact_id,
                "event_type": EXEMPT_ROW_RESTORED_EVENT,
                "actor_thread_id": None,
                "authority": "pane-persistence",
                "reason": None,
                "source_path": "registry.exemption_restore.restore_exempt_owner_row",
                "causal_event_id": None,
                "affected_thread_id": row["parent_thread_id"],
                "old_owner_thread_id": None,
                "new_owner_thread_id": None,
                "old_relationship_state": row["relationship_state"],
                "new_relationship_state": row["relationship_state"],
                "metadata": {
                    "trigger": trigger,
                    "old_surfacing": "settled",
                    "new_surfacing": "visible",
                },
                "now": now,
                "idempotency_scope": row["id"],
            }
            for row in rows
        ),
    )
    if rows:
        _nudge_after_commit(db, artifact_id, [str(row["parent_thread_id"]) for row in rows])
    return len(rows)


def _nudge_after_commit(db, artifact_id: str, owner_thread_ids: list[str]) -> None:
    # Imported here: plans imports the registry.
    from infra.write_effects import after_commit
    from plans.public import deliver_notifications

    messages = build_artifact_change_messages(
        artifact_id,
        owner_thread_id=owner_thread_ids[0],
        affected_thread_ids=owner_thread_ids,
        db=db,
    )
    after_commit(db, lambda: deliver_notifications(messages))
