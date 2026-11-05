"""Validation and inheritance of the agreed intent for plan nodes."""
from __future__ import annotations

from plans import service

MAX_INTENT_BYTES = 600


def validate_intent(value: object) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise service.PlanInputError("intent is required")
    if len(text.encode()) > MAX_INTENT_BYTES:
        raise service.PlanInputError(f"intent exceeds the {MAX_INTENT_BYTES}-byte limit")
    return text


def canonical(connection, node_id: str):
    row = service._node_row(connection, node_id)
    seen: set[str] = set()
    while row["intent_node_id"] is not None:
        if row["id"] in seen:
            raise service.PlanError("intent node reference cycle")
        seen.add(row["id"])
        row = service._node_row(connection, row["intent_node_id"])
    return row


def inherited_node_id(connection, plan_id: str) -> str | None:
    """Return the attached parent task's canonical intent for a worker plan."""
    owner = connection.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if owner is None or owner["owner_kind"] != "thread":
        return None
    parent = connection.execute(
        """SELECT id FROM plan_nodes WHERE spawned_thread_id=? AND archived_at IS NULL
           ORDER BY updated_at DESC LIMIT 1""", (owner["owner_id"],)
    ).fetchone()
    return None if parent is None else canonical(connection, parent["id"])["id"]


def detail(connection, node) -> dict:
    result = {"intent": node["intent"], "requirements_revision": node["requirements_revision"]}
    if node["intent_node_id"] is not None:
        source = canonical(connection, node["id"])
        result["canonical_intent"] = {
            "node_id": source["id"], "title": source["title"], "intent": source["intent"],
        }
    return result


def prepare(db, plan_id: str, params: dict, *, workflow_version: str | None, operation: str, actor, run: bool = False):
    """Validate one add/insert request before it can create a node."""
    from plans import boundary_fields as fields

    inherited = inherited_node_id(db, plan_id)
    created = fields.node_fields(
        params, workflow_version=workflow_version, include_before=operation == "insert",
        intent_required=inherited is None, run=run,
    )
    if inherited is not None and created["intent"] is None:
        created["intent_node_id"] = inherited
    return created
