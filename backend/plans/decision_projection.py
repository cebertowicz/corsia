"""Read-only projection over plan decisions held in artifact identities.

A decision *is* its `local/plan-decision` artifact identity: the structured body
and lifecycle live in that identity's ``plan_decision`` metadata object, and the
plan linkage is the existing ``plan_node_artifact_links`` `attention` row. The
candidate set for any read is therefore always bounded by one plan's nodes.
"""

from __future__ import annotations

import json

from infra.packed_rows import fetch_all
from plans import aggregates, hierarchy, node_states
from plans.decision_fields import pick_of

DECISION_TYPE = "plan-decision"
RAISED = "raised"
#: Lifecycle states a decision may occupy, in progression order.
STATES = ("draft", "parent_reviewed", "raised", "answered", "invalidated")
#: States that still hold a place in the user's queue.
OPEN_STATES = frozenset({"draft", "parent_reviewed", "raised"})
#: `pick` is deliberately absent: bodies stored before pick-any lack it and
#: must still project, as pick one (`decision_fields.pick_of`).
_BODY_REQUIRED = (
    "state", "order_key", "revision", "title", "context", "tradeoff",
    "recommendation", "rationale", "options", "other_enabled", "plan_id",
    "node_id", "source_node_id", "thread_id",
)

#: Reads that serve a task still in play ask only for decisions on open nodes
#: (``idx_plan_nodes_open``): a decision held by a node nothing is open beneath waits on
#: no one, and the read then costs the open work, not the history of the plan.
_OPEN_NODES = " AND n.folded=0"

# A question on closed work leaves the attention surface with it (#1887).
def _rows_sql(marks: str, *, compact: bool = False, open_nodes: bool = False) -> str:
    """Same eligible linked identities for full and narrow lifecycle consumers.

    CROSS JOIN pins the join order to the named plans' nodes, then their links,
    then the identity. Left to itself SQLite starts from every decision
    identity ever created and runs the retired-branch walk on each pairing,
    which made this read grow with all decisions instead of one plan's nodes.
    """
    columns = "i.id AS artifact_id,i.metadata,n.plan_id,l.node_id"
    columns += ",p.owner_id,t.spawned_by" if compact else ",i.external_id,i.created_at"
    open_clause = _OPEN_NODES if open_nodes else ""
    owners = "JOIN plans p ON p.id=n.plan_id LEFT JOIN threads t ON t.id=p.owner_id" if compact else ""
    return f"""
    SELECT {columns}
      FROM plan_nodes n
      CROSS JOIN plan_node_artifact_links l ON l.node_id = n.id
      CROSS JOIN artifact_identities i ON i.id = l.artifact_id
      {owners}
     WHERE l.role = 'attention'
       AND i.integration = 'local'
       AND i.type = ?
       AND i.state = 'active'
       AND {node_states.in_work_sql('n')}
       AND {node_states.outside_retired_branch_sql('n')}
       AND n.plan_id IN ({marks}){open_clause}
    """


def _body(row) -> dict | None:
    """Return one structurally complete decision body, or None to ignore it."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        return None
    body = metadata.get("plan_decision") if isinstance(metadata, dict) else None
    if not isinstance(body, dict):
        return None
    if any(field not in body for field in _BODY_REQUIRED):
        return None
    if body["state"] not in STATES or not isinstance(body["options"], list):
        return None
    return body


def _attention_state(row) -> str | None:
    """The pile's own state for this identity, alongside the decision body."""
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        return None
    return metadata.get("attention_state") if isinstance(metadata, dict) else None


def project(row, body: dict) -> dict:
    """Return the stable public representation of one decision."""
    return {
        "id": row["external_id"],
        "artifact_id": row["artifact_id"],
        "plan_id": body["plan_id"],
        "node_id": body["node_id"],
        "source_node_id": body["source_node_id"],
        "thread_id": body["thread_id"],
        "title": body["title"],
        "context": body["context"],
        "tradeoff": body["tradeoff"],
        "recommendation": body["recommendation"],
        "rationale": body["rationale"],
        "options": [dict(option) for option in body["options"]],
        "other_enabled": body["other_enabled"],
        "pick": pick_of(body),
        "order_key": body["order_key"],
        "state": body["state"],
        "revision": body["revision"],
        "created_at": row["created_at"],
        "raised_at": body.get("raised_at"),
        "answer": body.get("answer"),
        # Not published on the wire: the pile's state, so a caller can tell a
        # live question from one whose attention was cleared out of band.
        "attention_state": _attention_state(row),
    }


def identity_row(db, decision_id: str):
    """Return the active decision identity row, or None."""
    return db.execute(
        """SELECT id AS artifact_id, external_id, metadata, created_at
           FROM artifact_identities
           WHERE integration='local' AND type=? AND external_id=? AND state='active'""",
        (DECISION_TYPE, decision_id),
    ).fetchone()


def read(decision_id: str, *, db) -> dict:
    """Project one decision. Callers authorize first; this discloses nothing."""
    from plans import service

    row = identity_row(db, decision_id)
    body = _body(row) if row is not None else None
    if body is None:
        raise service.PlanError("unknown plan decision")
    return project(row, body)


def rows_for_plans(plan_ids, *, db, open_nodes: bool = False) -> list[dict]:
    """Return every well-formed decision attached to the given plans.

    ``open_nodes`` keeps the ones held by a node with open work in or at it.
    """
    unique = list(dict.fromkeys(plan_ids))
    if not unique:
        return []
    marks = ",".join("?" for _ in unique)
    open_nodes = open_nodes and aggregates.ready(db)
    rows = fetch_all(db, _rows_sql(marks, open_nodes=open_nodes), (DECISION_TYPE, *unique))
    projected = []
    for row in rows:
        body = _body(row)
        if body is not None:
            projected.append(project(row, body))
    return projected


def canonical_order(decisions: list[dict]) -> list[dict]:
    """Order the queue the user works through: agent priority, then age."""
    return sorted(
        decisions, key=lambda item: (item["order_key"], item["created_at"], item["id"]),
    )


def answerable(db, source_nodes: dict[str, str]) -> dict[str, bool]:
    """Which of these decision identities the answer route could actually settle.

    Mirrors ``decision_authz.resolve``: a live `attention` link, a live review
    node, a live **source** node, a non-archived thread plan, and a live owner
    thread. One statement bounded by the caller's batch, so the cost never grows
    with the number of decisions ever created.

    It also requires the review node to still be in the one state ``answer``
    can settle. Authorization is not the only way a question stops being
    settleable: the node's own state machine is the other, and a row that lost
    that state must never become a head that starves every later decision.

    Shared by the user's queue and the plan's indicator so the two can never
    disagree about whether a question is still live.
    """
    if not source_nodes:
        return {}
    marks = ",".join("?" for _ in source_nodes)
    rows = db.execute(
        f"""SELECT l.artifact_id AS artifact_id, src.id AS source_node_id
              FROM plan_node_artifact_links l
              JOIN plan_nodes n ON n.id = l.node_id AND n.archived_at IS NULL
                                AND n.state = 'review_requested'
              JOIN plans p ON p.id = n.plan_id AND p.state != 'archived'
              JOIN threads s ON s.id = p.owner_id
              JOIN plan_nodes src
                ON src.plan_id = p.id AND {node_states.in_work_sql('src')}
               AND {node_states.outside_retired_branch_sql('src')}
             WHERE l.role = 'attention'
               AND p.owner_kind = 'thread'
               AND s.archived_at IS NULL AND s.purged_at IS NULL
               AND COALESCE(s.lifecycle_state,'active') != 'archived'
               AND l.artifact_id IN ({marks})""",
        tuple(sorted(source_nodes)),
    ).fetchall()
    live = {(row["artifact_id"], row["source_node_id"]) for row in rows}
    return {
        artifact_id: (artifact_id, source_node_id) in live
        for artifact_id, source_node_id in source_nodes.items()
    }


def raised_decisions(plan_ids, *, db, open_nodes: bool = False) -> list[dict]:
    """Return the decisions actually awaiting the user, in canonical order.

    A decision counts only when its body says raised, the pile agrees, and the
    answer route could settle it. Reading the body alone let a plan keep
    counting a question whose attention had been cleared out of band.
    """
    rows = [
        item for item in rows_for_plans(plan_ids, db=db, open_nodes=open_nodes)
        if item["state"] == RAISED
    ]
    live = answerable(
        db, {item["artifact_id"]: item["source_node_id"] for item in rows},
    )
    return canonical_order([
        item for item in rows
        if item["attention_state"] == RAISED and live.get(item["artifact_id"])
    ])


def activity_facts(plan_ids, *, db) -> list[dict]:
    """Exact owner grammar and answerability, returning lifecycle facts only.

    Each plan chunk's eligible decision rows come back in one stepped read, and
    their metadata is parsed with the existing persisted reader. Work is linear
    in those bodies; no body/options/context escapes this owner and no full
    renderer is invoked.
    """
    unique = list(dict.fromkeys(str(plan_id) for plan_id in plan_ids))
    facts: list[dict] = []
    for start in range(0, len(unique), hierarchy._ID_CHUNK):
        chunk = unique[start:start + hierarchy._ID_CHUNK]
        rows = fetch_all(
            db,
            _rows_sql(",".join("?" for _ in chunk), compact=True),
            (DECISION_TYPE, *chunk),
        )
        for row in rows:
            body = _body(row)
            if body is None:
                continue
            facts.append({
                key: row[key] for key in ("artifact_id", "plan_id", "node_id", "owner_id", "spawned_by")
            } | {
                "decision_state": body["state"], "source_node_id": body["source_node_id"],
                "attention_state": _attention_state(row),
            })
    raised = {
        item["artifact_id"]: item["source_node_id"]
        for item in facts if item["decision_state"] == RAISED
    }
    live: dict[str, bool] = {}
    identities = list(raised)
    for start in range(0, len(identities), hierarchy._ID_CHUNK):
        chunk = identities[start:start + hierarchy._ID_CHUNK]
        live.update(answerable(db, {key: raised[key] for key in chunk}))
    for item in facts:
        item["answerable"] = bool(
            item["decision_state"] == RAISED and item["attention_state"] == RAISED
            and live.get(item["artifact_id"])
        )
    return facts


#: The exact decision fields published to a renderer. Registry ids and recorded
#: answers stay server-side.
WIRE_FIELDS = (
    "id", "plan_id", "node_id", "source_node_id", "thread_id", "title",
    "context", "tradeoff", "recommendation", "rationale", "options",
    "other_enabled", "pick", "order_key", "revision", "raised_at",
)


def awaiting_count(plan_ids, *, db) -> int:
    """Count raised decisions awaiting the user on these plans or any below.

    A parent genuinely awaits a question its spawned child raised, so a count
    that stopped at the plan's own rows would read zero while the user's queue
    showed the decision. Bounded by the plan subtree, not the workspace.

    Named for what it is: PRD-PLANS E6 says a decision does not pause the
    thread, so these plans are waiting on the user, not blocked.

    A decision is held by a node in work, and a node in work is never folded, so
    the read asks for the open rows alone (``idx_plan_nodes_open``) and costs the
    open work, not the plan's history.
    """
    rows = hierarchy.descendants(db, plan_ids)
    return len(raised_decisions([row["id"] for row in rows], db=db, open_nodes=True))


def next_order_key(plan_id: str, *, db) -> int:
    """Return the append position among a plan's still-open decisions."""
    open_keys = [
        item["order_key"] for item in rows_for_plans([plan_id], db=db)
        if item["state"] in OPEN_STATES
    ]
    return max(open_keys) + 1 if open_keys else 0
