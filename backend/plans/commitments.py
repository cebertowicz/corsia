"""Original-commitment lineage for plan creation and usage attribution."""

from __future__ import annotations

import plans.service as service

_OPEN_STATES = ("pending", "active", "review_requested")


def attached_parent_node(plan_id: str, *, db):
    """Return the one live task whose structured attachment owns this child plan.

    Consumed contract. ``infra.migration_7_to_8_lineage`` calls this while the
    database is still at v7, so one definition of attachment serves both the
    runtime and that frozen migration. Changing the *rule* is intended: the
    migration follows CORSIA's current definition rather than keeping a
    private copy, because a copy is what shipped a weaker predicate and
    aborted a production release.

    What the migration cannot survive is a change to the *data this reads*.
    Every column below exists at v7, and reading one that does not would stop
    an existing v7 database from migrating at all:

    - ``plans.owner_kind``, ``owner_id``, ``parent_plan_id``
    - ``plan_nodes.plan_id``, ``spawned_thread_id``, ``spawn_kind``,
      ``archived_at``, ``state``
    - ``plan_node_events.event``, ``payload_json`` (``$.child_plan_id``)

    Raising ``PlanError`` on more than one live attachment is part of the
    contract too; the migration reports which child plan failed rather than
    guessing. If a future rule needs state that v7 lacks, snapshot the answer
    into the migration instead of extending this function.
    ``test_attachment_resolves_against_a_v7_database`` fails when this breaks.
    """

    plan = service._plan_row(db, plan_id)
    if (
        plan["owner_kind"] != "thread"
        or plan["parent_plan_id"] is None
    ):
        return None
    placeholders = ",".join("?" for _ in _OPEN_STATES)
    rows = db.execute(
        f"""SELECT n.* FROM plan_nodes n
            WHERE n.plan_id=? AND n.spawned_thread_id=?
              AND n.spawn_kind IS NOT NULL AND n.archived_at IS NULL
              AND n.state IN ({placeholders})
              AND EXISTS (
                SELECT 1 FROM plan_node_events e
                WHERE e.node_id=n.id AND e.event='spawned_thread_attached'
                  AND json_valid(e.payload_json)
                  AND json_extract(e.payload_json,'$.child_plan_id')=?
              )
            ORDER BY n.id""",
        (
            plan["parent_plan_id"],
            plan["owner_id"],
            *_OPEN_STATES,
            plan_id,
        ),
    ).fetchall()
    if len(rows) > 1:
        raise service.PlanError("child plan has multiple live task attachments")
    return rows[0] if rows else None


def resolve_for_creation(
    plan_id: str,
    node_id: str,
    *,
    parent_id: str | None,
    explicit_commitment_id: str | None,
    db,
) -> str:
    """Resolve a node's immutable original obligation from canonical lineage."""

    if explicit_commitment_id is not None:
        if explicit_commitment_id == node_id:
            return node_id
        source = service._node_row(db, explicit_commitment_id)
        if source["commitment_id"] != explicit_commitment_id:
            raise service.PlanError("commitment source is not an original obligation")
        return explicit_commitment_id
    if parent_id is not None:
        return str(service._node_row(db, parent_id)["commitment_id"])
    attachment = attached_parent_node(plan_id, db=db)
    if attachment is not None:
        return str(attachment["commitment_id"])
    return node_id


def usage_role(node_id: str, *, db) -> str:
    """Classify one selected task without adding another persisted role owner."""

    correction = db.execute(
        """SELECT 1 FROM plan_node_events
           WHERE node_id=? AND event='correction_created' LIMIT 1""",
        (node_id,),
    ).fetchone()
    if correction is not None:
        return "correction"
    node = service._node_row(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    if plan["parent_plan_id"] is not None and node["commitment_id"] != node["id"]:
        return "delegation"
    return "execution"


def inherit_open_child_nodes(
    child_plan_id: str,
    parent_node_id: str,
    *,
    actor_id: str | None,
    provenance: str,
    db,
) -> None:
    """Bind the child's open work that has not run yet to the parent's commitment.

    A step that already ran keeps its own commitment. Its attempts and its
    model usage were recorded against that commitment, and rebinding the
    step would move the attempts (read through the node) without the usage
    (stamped at write time), splitting one step's cost across two lines.
    So a child attached mid-work -- a ``delegate_task`` child, a replacement
    worker -- attaches, and only the steps it has not started, and every step
    it creates from now on, roll up under the parent.
    """

    parent = service._node_row(db, parent_node_id)
    child_plan = service._plan_row(db, child_plan_id)
    if (
        parent["archived_at"] is not None
        or parent["state"] not in _OPEN_STATES
        or child_plan["parent_plan_id"] != parent["plan_id"]
        or child_plan["owner_kind"] != "thread"
        or parent["spawned_thread_id"] != child_plan["owner_id"]
    ):
        raise service.PlanError("child commitment requires a live structured attachment")
    rows = db.execute(
        """SELECT * FROM plan_nodes
           WHERE plan_id=? AND archived_at IS NULL
             AND state IN ('pending','active','review_requested')
           ORDER BY order_key,id""",
        (child_plan_id,),
    ).fetchall()
    node_ids = [row["id"] for row in rows]
    if not node_ids:
        return
    placeholders = ",".join("?" for _ in node_ids)
    ran = {
        str(row[0])
        for row in db.execute(
            f"""SELECT node_id FROM plan_node_attempts WHERE node_id IN ({placeholders})
                UNION SELECT plan_node_id FROM llm_usage
                WHERE plan_node_id IN ({placeholders})""",
            (*node_ids, *node_ids),
        ).fetchall()
    }
    now = service._now()
    for row in rows:
        if row["id"] in ran or row["commitment_id"] == parent["commitment_id"]:
            continue
        service.touch_node(db, row["id"], at=now, commitment_id=parent["commitment_id"])
        service._event(
            db,
            row["id"],
            "commitment_attached",
            actor_id=actor_id,
            provenance=provenance,
            payload={
                "parent_node_id": parent_node_id,
                "commitment_id": parent["commitment_id"],
            },
        )
