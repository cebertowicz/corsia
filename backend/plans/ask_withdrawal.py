"""Withdrawal orchestration for artifact asks a task no longer owns."""

from __future__ import annotations

from plans.node_states import open_user_block_sql


def end_ask(
    db, node_id: str, hold_id: str, artifact_id: str, *, actor_id: str | None, provenance: str,
) -> bool:
    """End one ask as its actor did, then close its artifact attention if it was last.

    A user acting on their own wait acknowledges it; every other actor, including
    a system migration, withdraws it. This is the only ending rule for an ask.
    """
    from plans import holds
    from plans.boundary import is_user_audit_id

    withdrawn = not is_user_audit_id(actor_id)
    holds.clear_write(
        node_id, hold_id, expected_kind="blocked", actor_id=actor_id,
        provenance=provenance, detail={"ended": "withdrawn"} if withdrawn else None, db=db,
    )
    clear_ended_ask_attention(db, artifact_id, withdrawn=withdrawn)
    return withdrawn


def withdraw_node_asks(
    db, node_id: str, *, actor_id: str | None, provenance: str,
    keep_artifact_id: str | None = None,
) -> int:
    """Withdraw open artifact asks on one task, optionally preserving one replacement.

    The hold records the ask's lifecycle edge; the registry owns the artifact's
    attention state. Clear the former first, then clear the latter only when no
    other open ask still refers to the artifact (legacy duplicate asks remain
    visible rather than being silently removed).
    """
    rows = db.execute(
        f"""SELECT h.id AS hold_id, h.owner_id AS artifact_id
              FROM plan_node_holds h
              JOIN plan_node_artifact_links l
                ON l.node_id=h.node_id AND l.artifact_id=h.owner_id AND l.role='attention'
             WHERE h.node_id=? AND {open_user_block_sql('h')} AND h.owner_id IS NOT NULL
               AND (? IS NULL OR h.owner_id<>?)
             ORDER BY h.created_at,h.id""",
        (node_id, keep_artifact_id, keep_artifact_id),
    ).fetchall()
    for row in rows:
        end_ask(
            db, node_id, str(row["hold_id"]), str(row["artifact_id"]),
            actor_id=actor_id, provenance=provenance,
        )
    return len(rows)


def clear_ended_ask_attention(db, artifact_id: str, *, withdrawn: bool = True) -> None:
    """Clear the artifact pile entry only after its last open ask ends.

    An agent ending its own ask withdraws it; the user resolving it acknowledges it.
    """
    from plans import user_ask

    if user_ask.raised_asks(db, artifact_id):
        return
    from registry import attention

    if withdrawn:
        attention.withdraw_attention(artifact_id, db=db)
    else:
        attention.clear_attention(artifact_id, outcome="acknowledge", db=db)
