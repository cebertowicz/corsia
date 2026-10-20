"""How one decision mutation is applied, receipted and deferred.

The write plumbing behind ``plans.decisions``: rebuilding a decision's stored
body, persisting it with its audit event, re-admitting its plan, and the shared
authorize → replay-check → apply → receipt → defer shape every decision verb
runs inside one ``service._write`` transaction. The verbs themselves — which
mutations exist and what each is allowed to change — stay in ``plans.decisions``.
"""

from __future__ import annotations

import plans.artifact_links as artifact_links
import plans.decision_authz as authz
import plans.decision_projection as decision_projection
import plans.intent_words as intent_words
import plans.notifications as notifications
import plans.receipts as receipts
import plans.service as service
from registry import models

TYPE = decision_projection.DECISION_TYPE
REVIEW_DOD = (
    "The user answers this decision and the answer is recorded on this plan."
)

read = decision_projection.read


def _touch_plan(db, plan_id: str) -> None:
    """Advance plan freshness for changes the plan-node triggers cannot see."""
    db.execute(
        "UPDATE plans SET revision=revision+1, updated_at=? WHERE id=?",
        (service._now(), plan_id),
    )


def _body_from(decision: dict, **changes) -> dict:
    """Rebuild the stored body from its projection, bumping the revision once."""
    body = {
        "version": 1,
        "plan_id": decision["plan_id"],
        "node_id": decision["node_id"],
        "source_node_id": decision["source_node_id"],
        "thread_id": decision["thread_id"],
        "title": decision["title"],
        "context": decision["context"],
        "tradeoff": decision["tradeoff"],
        "recommendation": decision["recommendation"],
        "rationale": decision["rationale"],
        "options": decision["options"],
        "other_enabled": decision["other_enabled"],
        "pick": decision["pick"],
        "order_key": decision["order_key"],
        "state": decision["state"],
        "revision": decision["revision"] + 1,
        "raised_at": decision["raised_at"],
        "answer": decision["answer"],
    }
    body.update(changes)
    return body


def _commit(db, decision, body, event, actor, provenance, payload=None) -> dict:
    """Persist one new decision body with its audit event and plan freshness."""
    models.update_metadata(
        "local", TYPE, decision["id"], {"plan_decision": body},
        db=db,
    )
    if body["title"] != decision["title"]:
        db.execute(
            "UPDATE artifact_identities SET title=? WHERE id=?",
            (body["title"], decision["artifact_id"]),
        )
    service._event(
        db, decision["node_id"], event, actor_id=actor.audit_id,
        provenance=provenance, payload=payload,
    )
    _touch_plan(db, decision["plan_id"])
    _reconcile(db, decision["plan_id"], actor, provenance)
    return {"decision": read(decision["id"], db=db)}


def _reconcile(db, plan_id: str, actor, provenance: str) -> None:
    """Re-admit the plan in this same write: a decision is its source task's wait."""
    from plans import admission

    admission.reconcile_plan(
        plan_id, actor_id=actor.audit_id, provenance=provenance,
        causing_executor=actor.thread_id if actor.kind == "thread" else None,
        db=db,
    )


def _perform(decision_id, operation, params, *, mutation_id, actor, provenance,
             db, apply, settled_source_ok=False):
    """Authorize, replay-check, apply, receipt, and defer one mutation."""
    mutation_id = authz.required_mutation_id(mutation_id)
    changed = False
    message_floor = None
    drives: tuple[str, ...] = ()
    with notifications.collect_touched() as touched, service._write(db):
        plan, decision, source, bound = authz.resolve(
            db, decision_id, actor, settled_source_ok=settled_source_ok,
        )
        message_floor = notifications.capture_message_floor(db)
        from registry.node_retirement import capture_event_floor

        artifact_event_floor = capture_event_floor(db)
        fingerprint = receipts.request_fingerprint(
            operation, {"decision_id": decision["id"], **params},
        )
        replay = receipts.load(
            db, plan_id=plan["id"], mutation_id=mutation_id,
            actor_id=bound.audit_id, operation=operation, fingerprint=fingerprint,
        )
        if replay is not None:
            result = replay
        else:
            result = apply(db, plan, decision, source, bound)
            receipts.record(
                db, plan_id=plan["id"], mutation_id=mutation_id,
                actor_id=bound.audit_id, operation=operation,
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
    # A replayed answer appends nothing, and its durable input may still be
    # waiting on a drive that failed the first time, so the owner is driven
    # again. Every other replayed operation has nothing for anyone to act on.
    return notifications.DeferredMutation(
        result, (),
        drives=(
            (str(result["decision"]["thread_id"]),)
            if operation == "decision_answer"
            else ()
        ),
    )


def _create(db, plan, source, author, content, actor, provenance) -> dict:
    """Create the identity, its user-assigned review node, and their link."""
    decision_id = service._id()
    node = service.add_node(
        plan["id"], kind="task", title=content["title"],
        acceptance_criteria=REVIEW_DOD, description=content["context"],
        parent_id=source["parent_id"], assignee="user",
        intent_node_id=str(intent_words.canonical(db, source["id"])["id"]),
        actor_id=actor.audit_id, provenance=provenance, db=db,
    )
    body = {
        "version": 1, "plan_id": plan["id"], "node_id": node.id,
        "source_node_id": source["id"], "thread_id": author,
        "state": "draft", "revision": 1, "raised_at": None, "answer": None,
        "order_key": decision_projection.next_order_key(plan["id"], db=db),
        **content,
    }
    models.register_artifact(
        integration="local", type=TYPE, external_id=decision_id,
        parent_thread_id=artifact_links._attention_owner(
            db, service._node_row(db, node.id),
        ),
        title=content["title"], metadata={"plan_decision": body},
        db=db,
    )
    artifact_links.link(
        node.id, decision_projection.identity_row(db, decision_id)["artifact_id"],
        "attention", actor_id=actor.audit_id, provenance=provenance, db=db,
    )
    service._event(
        db, node.id, "decision_drafted", actor_id=actor.audit_id,
        provenance=provenance, payload={"decision_id": decision_id},
    )
    _touch_plan(db, plan["id"])
    _reconcile(db, plan["id"], actor, provenance)
    return {"decision": read(decision_id, db=db)}
