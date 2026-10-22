"""The intent gate and provenance writes for task creation."""
from __future__ import annotations

from plans import intent_words, service


def prepare(db, plan_id: str, params: dict, *, workflow_version: str | None, operation: str, actor):
    """Validate one add/insert request before it can create a node."""
    from plans import boundary_fields as fields

    inherited = intent_words.inherited_node_id(db, plan_id)
    created = fields.node_fields(
        params, workflow_version=workflow_version, include_before=operation == "insert",
        intent_required=inherited is None,
    )
    requested_words = created.pop("intent_words", None)
    acting = actor.thread_id if actor.kind == "thread" else None
    resolved_words = intent_words.resolve_words(db, requested_words, actor_id=acting)
    if (
        actor.kind == "thread" and created["kind"] == "task" and inherited is None
        and not intent_words.has_person_origin(db, actor.turn_id)
        and not any(word["author"] == "person" and word["role"] == "authority" for word in resolved_words)
    ):
        raise service.PlanInputError(
            "task creation needs intent_words with an exact person quote and role: authority"
        )
    if inherited is not None and created["intent"] is None:
        created["intent_node_id"] = inherited
    return created, resolved_words


def capture(db, node, words: list[dict[str, str]], *, actor) -> None:
    """Record the turn's origin and resolved caller words at node revision one."""
    intent_words.capture_origin(db, node.id, turn_id=actor.turn_id, revision=node.requirements_revision)
    intent_words.append(db, node.id, words, revision=node.requirements_revision)
