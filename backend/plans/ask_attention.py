"""Withdrawal orchestration for artifact asks a task no longer owns, and the attention they leave.

An open review ask is held in two stores: the plan hold, and ``attention_state``
on its artifact, which plan decisions and parent review also raise. No ask edge
decides that attention write on its own. Placing or restating an ask, and ending
or withdrawing one, each call ``sync_ask_attention``, which reads every live
cause raising the artifact and writes through ``registry.attention``, the
state's one writer, only where the pile disagrees with them.
"""

from __future__ import annotations

from plans.node_states import open_user_block_sql

#: Pile states that keep the artifact in front of the user: raised, or deferred
#: by the user (or a parent review) until a condition re-raises it.
_HELD = frozenset({"raised", "deferred"})
#: How an ask edge ends attention nothing else raises, by who ended the ask.
_ENDINGS = frozenset({"withdraw", "acknowledge"})


def end_ask(
    db, node_id: str, hold_id: str, artifact_id: str, *, actor_id: str | None, provenance: str,
) -> bool:
    """End one ask as its actor did, then bring its artifact's attention in line.

    A user acting on their own wait acknowledges it; every other actor, including
    a system migration, withdraws it. This is the only ending rule for an ask
    (``ending_outcome``). The artifact's attention closes only when nothing else
    still raises it (``sync_ask_attention``).
    """
    from plans import holds

    outcome = ending_outcome(actor_id)
    withdrawn = outcome == "withdraw"
    holds.clear_write(
        node_id, hold_id, expected_kind="blocked", actor_id=actor_id,
        provenance=provenance, detail={"ended": "withdrawn"} if withdrawn else None, db=db,
    )
    sync_ask_attention(db, artifact_id, outcome=outcome)
    return withdrawn


def withdraw_node_asks(
    db, node_id: str, *, actor_id: str | None, provenance: str,
    keep_artifact_id: str | None = None,
) -> int:
    """Withdraw open artifact asks on one task, optionally preserving one replacement.

    The hold records the ask's lifecycle edge; the registry owns the artifact's
    attention state. Clear the former first, then reconcile the latter: it stays
    raised while anything else still raises the artifact, such as another open
    ask (legacy duplicate asks remain visible rather than being silently
    removed), a parent review's approval, or a raised decision.
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


def ending_outcome(actor_id: str | None) -> str:
    """``acknowledge`` when the user ends their own wait; ``withdraw`` for every other actor."""
    from plans.boundary import is_user_audit_id

    return "acknowledge" if is_user_audit_id(actor_id) else "withdraw"


def sync_ask_attention(db, artifact_id: str, *, outcome: str) -> str | None:
    """Bring one artifact's attention in line with its live raise causes.

    The only place an ask edge touches attention. The causes are an open user
    ask on the artifact (``user_ask.raised_asks``); a plan decision the user's
    queue still counts (``decision_projection.raised_decisions``); and a parent
    review that sent the artifact to the user or deferred it there
    (``approve_to_user``, ``defer_until``) while the pile still holds it.

    With a cause, attention in any state but raised or deferred is raised, and
    raised or deferred attention is left as it is, so a deferral stands. With
    none, raised or deferred attention ends with ``outcome``: ``withdraw`` when
    an agent or the system ended the ask, ``acknowledge`` when the user did.
    Returns the state written, or None when nothing was written. An artifact
    with no identity row has no attention to reconcile.
    """
    if outcome not in _ENDINGS:
        raise ValueError(f"not an ask ending: {outcome!r}")
    from plans import user_ask
    from registry import attention
    from registry.identity_metadata import parse_metadata

    row = db.execute(
        "SELECT id,type,external_id,metadata FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        return None
    metadata = parse_metadata(row["metadata"])
    held = metadata.get("attention_state") in _HELD
    if held and _parent_review_raises(metadata):
        return None
    raised = bool(user_ask.raised_asks(db, artifact_id)) or _decision_raises(db, row)
    if raised and not held:
        return attention.raise_attention(artifact_id, db=db)
    if held and not raised:
        if outcome == "withdraw":
            return attention.withdraw_attention(artifact_id, db=db).attention_state
        return attention.clear_attention(artifact_id, outcome=outcome, db=db).attention_state
    return None


def _parent_review_raises(metadata: dict) -> bool:
    """Whether a parent review sent the artifact to the user, or deferred it until a condition.

    Read only while the pile holds the artifact: the disposition stays in
    metadata after the user clears it, so on its own it proves nothing. A raise
    that starts a new episode drops it (``registry.attention.raise_attention``),
    and a user's defer keeps it, so while the pile holds the artifact its
    disposition was written in the episode now holding it. Attention raised
    before raises dropped it can still carry one from an earlier episode.
    """
    from parent_review.disposition import APPROVE_TO_USER, DEFER_UNTIL

    return metadata.get("disposition_outcome") in (APPROVE_TO_USER, DEFER_UNTIL)


def _decision_raises(db, row) -> bool:
    """Whether the artifact is a decision the user's queue still counts.

    Decided by ``decision_projection.raised_decisions``, the queue's own reader,
    over the decision's plan, so the pile and the queue cannot disagree.
    """
    from plans import decision_projection, service

    if row["type"] != decision_projection.DECISION_TYPE:
        return False
    try:
        decision = decision_projection.read(row["external_id"], db=db)
    except service.PlanError:
        return False
    return any(
        item["artifact_id"] == row["id"]
        for item in decision_projection.raised_decisions([decision["plan_id"]], db=db)
    )
