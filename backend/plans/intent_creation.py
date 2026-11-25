"""Validation and inheritance of the agreed intent for plan nodes."""
from __future__ import annotations

from plans import node_states, service

MAX_INTENT_BYTES = 600

# Both creation and the operator projection use the same parent choice and chain.
def delegated_parent_sql(owner_id: str) -> str:
    """Select the most recently updated unarchived node spawning an owner."""
    return f"""SELECT parent.id FROM plan_nodes parent
        WHERE parent.spawned_thread_id = {owner_id} AND {node_states.unarchived_sql('parent')}
        ORDER BY parent.updated_at DESC LIMIT 1"""


CANONICAL_CHAIN_SQL = """chain(opened_id, source_id) AS (
    SELECT opened_id, source_id FROM intent_roots
    UNION
    SELECT chain.opened_id, n.intent_node_id FROM chain
    JOIN plan_nodes n ON n.id = chain.source_id
    WHERE n.intent_node_id IS NOT NULL
)"""


def validate_intent(value: object) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise service.PlanInputError("intent is required")
    if len(text.encode()) > MAX_INTENT_BYTES:
        raise service.PlanInputError(f"intent exceeds the {MAX_INTENT_BYTES}-byte limit")
    return text


def canonical(connection, node_id: str):
    row = connection.execute(
        f"""WITH RECURSIVE intent_roots(opened_id, source_id) AS (VALUES (?, ?)),
        {CANONICAL_CHAIN_SQL}
        SELECT n.* FROM chain CROSS JOIN plan_nodes n ON n.id = chain.source_id
        WHERE n.intent_node_id IS NULL""", (node_id, node_id),  # CROSS JOIN: walk the chain, never scan plan_nodes
    ).fetchone()
    if row is not None:
        return row
    # Distinguish a dangling reference from a cycle without a second chain rule.
    missing = connection.execute(
        f"""WITH RECURSIVE intent_roots(opened_id, source_id) AS (VALUES (?, ?)),
        {CANONICAL_CHAIN_SQL}
        SELECT chain.source_id FROM chain LEFT JOIN plan_nodes n ON n.id = chain.source_id
        WHERE n.id IS NULL""", (node_id, node_id),
    ).fetchone()
    if missing is not None:
        raise service.PlanError(f"unknown plan node: {missing['source_id']}")
    raise service.PlanError("intent node reference cycle")


def inherited_node_id(connection, plan_id: str) -> str | None:
    """Return the attached parent task's canonical intent for a worker plan."""
    owner = connection.execute("SELECT owner_kind,owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()
    if owner is None or owner["owner_kind"] != "thread":
        return None
    parent = connection.execute(
        delegated_parent_sql("?"), (owner["owner_id"],)
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
    parent_id = params.get("parent_id")
    if params.get("kind", "task") == "task" and isinstance(parent_id, str) and params.get("intent") is None:
        parent = db.execute(
            "SELECT id FROM plan_nodes WHERE id=? AND plan_id=? AND kind='task' AND workflow_step IS NOT NULL",
            (parent_id, plan_id),
        ).fetchone()
        if parent is not None:
            inherited = canonical(db, parent["id"])["id"]
    created = fields.node_fields(
        params, workflow_version=workflow_version, include_before=operation == "insert",
        intent_required=inherited is None, run=run,
    )
    if inherited is not None and created["intent"] is None:
        created["intent_node_id"] = inherited
    return created
