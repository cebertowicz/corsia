"""Withdrawal orchestration for artifact asks a task no longer owns."""

from __future__ import annotations

from plans.node_states import open_user_block_sql


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
    from plans import holds

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
        holds.clear_write(
            node_id, str(row["hold_id"]), expected_kind="blocked", actor_id=actor_id,
            provenance=provenance, detail={"ended": "withdrawn"}, db=db,
        )
    for artifact_id in dict.fromkeys(str(row["artifact_id"]) for row in rows):
        clear_ended_ask_attention(db, artifact_id)
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
