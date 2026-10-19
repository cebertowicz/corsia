"""Actor-bound lifecycle for structured plan decisions (PRD-PLANS E1-E8).

A decision is one `local/plan-decision` artifact identity — not a second queue.
Its structured body and lifecycle live in that identity's ``plan_decision``
metadata; its place in the plan is the existing ``plan_node_artifact_links``
`attention` row; and "this needs the user" is only ever ``registry.attention``,
the one pile. Each decision also owns one user-assigned review node, which is
what an answer settles.

Every mutation authorizes (``plans.decision_authz``) before disclosing
existence, runs inside one ``service._write`` transaction, is revision-checked
and receipt-idempotent, and returns post-commit invalidation facts through
``plans.notifications``.
"""

from __future__ import annotations

import json

import plans.decision_authz as authz
import plans.decision_delivery as decision_delivery
import plans.decision_fields as decision_fields
import plans.decision_lifecycle as decision_lifecycle
import plans.decision_projection as decision_projection
import plans.notifications as notifications
import plans.receipts as receipts
import plans.service as service
from plans.decision_writes import (
    REVIEW_DOD,
    TYPE,
    _body_from,
    _commit,
    _create,
    _perform,
    _touch_plan,
)
from registry import attention

#: Content and ordering stay changeable while the user has not answered (E8).
MUTABLE_STATES = ("draft", "parent_reviewed", "raised")

read = decision_projection.read

__all__ = [
    "MUTABLE_STATES", "REVIEW_DOD", "TYPE", "answer", "draft", "invalidate",
    "parent_review", "raise_to_user", "read", "reorder", "update",
]


# --- operations --------------------------------------------------------------


def draft(plan_id: str, *, source_node_id, mutation_id: str, actor, provenance,
          db, **fields) -> notifications.DeferredMutation:
    """Draft one structured decision against an accessible thread plan node."""
    content = decision_fields.content(fields)
    mutation_id = authz.required_mutation_id(mutation_id)
    changed = False
    message_floor = None
    drives: tuple[str, ...] = ()
    with notifications.collect_touched() as touched, service._write(db):
        plan, bound = authz.authorized_plan(db, plan_id, actor)
        message_floor = notifications.capture_message_floor(db)
        from registry.node_retirement import capture_event_floor

        artifact_event_floor = capture_event_floor(db)
        source = authz.source_node(db, plan["id"], source_node_id)
        authz.require_author(db, plan, source, bound)
        author, _reviewer = authz.tiers(db, plan)
        fingerprint = receipts.request_fingerprint(
            "decision_draft", {"source_node_id": source["id"], **content},
        )
        replay = receipts.load(
            db, plan_id=plan["id"], mutation_id=mutation_id,
            actor_id=bound.audit_id, operation="decision_draft",
            fingerprint=fingerprint,
        )
        if replay is not None:
            result = replay
        else:
            result = _create(db, plan, source, author, content, bound, provenance)
            receipts.record(
                db, plan_id=plan["id"], mutation_id=mutation_id,
                actor_id=bound.audit_id, operation="decision_draft",
                fingerprint=fingerprint, result=result,
            )
            changed = True
            drives = notifications.wake_recipients(message_floor, db=db)
    if changed:
        return notifications.deferred_mutation(
            result,
            [plan["id"], *touched],
            db=db,
            message_floor=message_floor,
            artifact_event_floor=artifact_event_floor,
            drives=drives,
        )
    return notifications.DeferredMutation(result, ())


def update(decision_id: str, *, expected_revision, mutation_id, actor, provenance,
           db, **patch) -> notifications.DeferredMutation:
    """Rewrite decision content, returning it to draft for a fresh review (E5)."""

    def apply(db, plan, decision, source, bound):
        authz.require_author_or_reviewer(db, plan, source, bound)
        authz.require_revision(decision, expected_revision)
        authz.require_state(decision, MUTABLE_STATES)
        content = decision_fields.patched(decision, patch)
        if decision["state"] == "raised":
            attention.clear_attention(
                decision["artifact_id"], outcome="supersede",
                reason="decision content superseded", db=db,
            )
            service.transition_node(
                decision["node_id"], "active", actor_id=bound.audit_id,
                provenance=provenance, db=db,
            )
        body = _body_from(decision, state="draft", raised_at=None, **content)
        return _commit(db, decision, body, "decision_updated", bound, provenance,
                       payload={"fields": sorted(patch)})

    return _perform(decision_id, "decision_update", {"patch": sorted(patch.items())},
                    mutation_id=mutation_id, actor=actor, provenance=provenance,
                    db=db, apply=apply)


def reorder(decision_id: str, *, order_key, expected_revision, mutation_id, actor,
            provenance, db) -> notifications.DeferredMutation:
    """Change only a queued decision's canonical position (E8)."""

    def apply(db, plan, decision, source, bound):
        authz.require_author_or_reviewer(db, plan, source, bound)
        authz.require_revision(decision, expected_revision)
        authz.require_state(decision, MUTABLE_STATES)
        if isinstance(order_key, bool) or not isinstance(order_key, int) or order_key < 0:
            raise service.PlanError("order key must be a non-negative integer")
        body = _body_from(decision, order_key=order_key)
        return _commit(db, decision, body, "decision_reordered", bound, provenance,
                       payload={"order_key": order_key})

    return _perform(decision_id, "decision_reorder", {"order_key": order_key},
                    mutation_id=mutation_id, actor=actor, provenance=provenance,
                    db=db, apply=apply)


def parent_review(decision_id: str, *, expected_revision, mutation_id, actor,
                  provenance, db) -> notifications.DeferredMutation:
    """Record the parent tier's compliance review before any user raise (E5)."""

    def apply(db, plan, decision, source, bound):
        authz.require_reviewer(db, plan, source, bound)
        authz.require_revision(decision, expected_revision)
        authz.require_state(decision, ("draft",))
        body = _body_from(decision, state="parent_reviewed")
        return _commit(db, decision, body, "decision_parent_reviewed", bound,
                       provenance)

    return _perform(decision_id, "decision_parent_review", {},
                    mutation_id=mutation_id, actor=actor, provenance=provenance,
                    db=db, apply=apply)


def raise_to_user(decision_id: str, *, expected_revision, mutation_id, actor,
                  provenance, db) -> notifications.DeferredMutation:
    """Activate the one attention identity for a parent-reviewed decision."""

    def apply(db, plan, decision, source, bound):
        authz.require_reviewer(db, plan, source, bound)
        authz.require_revision(decision, expected_revision)
        authz.require_state(decision, ("parent_reviewed",))
        node = service._node_row(db, decision["node_id"])
        if node["state"] == "pending":
            service.transition_node(
                node["id"], "active", actor_id=bound.audit_id,
                provenance=provenance, db=db,
            )
        service.transition_node(
            decision["node_id"], "review_requested", actor_id=bound.audit_id,
            provenance=provenance, db=db,
        )
        attention.raise_attention(decision["artifact_id"], db=db)
        body = _body_from(decision, state="raised", raised_at=service._now())
        return _commit(db, decision, body, "decision_raised", bound, provenance)

    return _perform(decision_id, "decision_raise", {}, mutation_id=mutation_id,
                    actor=actor, provenance=provenance, db=db, apply=apply)


def invalidate(decision_id: str, *, expected_revision, mutation_id, actor,
               provenance, db) -> notifications.DeferredMutation:
    """Withdraw a decision, clearing the same attention identity it raised."""

    def apply(db, plan, decision, source, bound):
        authz.require_author_or_reviewer(db, plan, source, bound)
        authz.require_revision(decision, expected_revision)
        authz.require_state(decision, MUTABLE_STATES)
        if decision["state"] == "raised":
            attention.clear_attention(
                decision["artifact_id"], outcome="dismiss",
                reason="decision invalidated", db=db,
            )
        decision_lifecycle._close_review_node(
            db, decision["node_id"], actor_id=bound.audit_id, provenance=provenance,
        )
        body = _body_from(decision, state="invalidated", raised_at=None)
        return _commit(db, decision, body, "decision_invalidated", bound, provenance)

    return _perform(decision_id, "decision_invalidate", {},
                    mutation_id=mutation_id, actor=actor, provenance=provenance,
                    db=db, apply=apply, settled_source_ok=True)


def answer(decision_id: str, *, option_id=None, option_ids=None, other_text=None,
           expected_revision, mutation_id, actor, provenance,
           db) -> notifications.DeferredMutation:
    """Settle one raised decision: answer, attention, and review node together."""

    def apply(db, plan, decision, source, bound):
        authz.require_human(bound)
        authz.require_open_for_answer(decision)
        authz.require_revision(decision, expected_revision)
        chosen = decision_fields.answer_for(
            decision, option_id, option_ids, other_text,
        )
        from plans import review_evidence

        _end_user_pause(db, decision["node_id"], actor_id=bound.audit_id, provenance=provenance)
        review_evidence.submit(
            decision["node_id"],
            evidence=_answer_evidence(decision, chosen),
            expected_revision=service._node_row(db, decision["node_id"])["revision"],
            actor_id=bound.audit_id,
            provenance=provenance,
            db=db,
            paused_plan_binds=False,
        )
        service.transition_node(
            decision["node_id"], "accepted", actor_id=bound.audit_id,
            provenance=provenance, verification_result="passed",
            verified_by=bound.audit_id, db=db,
        )
        attention.clear_attention(
            decision["artifact_id"], outcome="answer",
            reason="decision answered", db=db,
        )
        body = _body_from(
            decision, state="answered", answer=chosen, answered_at=service._now(),
        )
        result = _commit(
            db, decision, body, "decision_answered", bound, provenance,
            payload=chosen,
        )
        decision_delivery.append_answer_input(db, decision, chosen)
        _note_answered_after_commit(
            db, decision["node_id"], decision_fields.answer_label(decision, chosen),
        )
        return result

    params = {"option_id": option_id, "other_text": other_text}
    if option_ids is not None:
        # Replays are keyed on the set, so the same mutation id with a
        # different set conflicts instead of returning the first answer.
        params["option_ids"] = (
            sorted({str(item).strip() for item in option_ids})
            if isinstance(option_ids, list) else option_ids
        )
    return _perform(decision_id, "decision_answer", params,
                    mutation_id=mutation_id, actor=actor, provenance=provenance,
                    db=db, apply=apply)


def _end_user_pause(db, node_id: str, *, actor_id: str, provenance: str) -> None:
    """The person's answer ends their own pause on the question it settles.

    A paused decision defers only its attention and stays answerable (pause
    contract rule 5); answering it is the person acting on the item, so their
    pause has nothing left to hold. Any other hold still refuses the answer.
    """
    from plans import holds

    for hold in holds.active_holds(node_id, db=db):
        if hold["kind"] == "paused" and hold["owner_kind"] == "user":
            holds.clear_write(
                node_id, hold["id"], expected_kind="paused", actor_id=actor_id,
                provenance=provenance, db=db,
            )


def _answer_evidence(decision: dict, chosen: dict) -> str:
    """The review node's evidence: pick one keeps its JSON; pick any reads aloud."""
    if decision_fields.pick_of(decision) != decision_fields.PICK_ANY:
        return json.dumps(chosen, sort_keys=True)
    labels = {option["id"]: option["label"] for option in decision["options"]}
    picked = [labels[option_id] for option_id in chosen["option_ids"]]
    evidence = f"Chosen: {', '.join(picked) if picked else 'none'}"
    if chosen["text"]:
        evidence += f" · Note: {json.dumps(chosen['text'], ensure_ascii=False)}"
    return evidence


def _note_answered_after_commit(db, node_id: str, option_id: str) -> None:
    """Onboarding measurement once the answer commits; never breaks the answer."""
    import logging

    from infra.onboarding_events import note_decision_answered
    from infra.write_effects import after_commit

    try:
        after_commit(db, lambda: note_decision_answered(node_id, option_id))
    except Exception:  # noqa: BLE001 - measurement only
        logging.getLogger(__name__).warning(
            "onboarding decision measurement not scheduled", exc_info=True,
        )
