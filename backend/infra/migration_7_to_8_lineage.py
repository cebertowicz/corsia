"""Which original commitment each plan node's cost belongs to, at schema v8.

Separated from the table mechanics in ``migration_7_to_8_plans`` because it
answers a different question: not "what shape do these rows take" but "whose
commitment does this node inherit".

Lineage flows from four sources: a node is its own commitment unless it was
split from another, is the follow-up of an accepted-with-follow-up node, or is
open work inside a child plan attached to a live parent task. The first three
are read straight out of the v7 data. The fourth is a live semantic, so this
module asks the runtime rather than deciding for itself; see
``_child_attachments``.
"""

from __future__ import annotations

import json
import sqlite3

from infra.migration_errors import PlanMigrationError


def _child_attachments(db: sqlite3.Connection) -> dict[str, str]:
    """Map each child plan to the one live parent task it hangs from.

    Delegates to ``plans.commitments.attached_parent_node`` instead of running
    its own query, so "which parent task owns this child plan" has exactly one
    definition in the codebase.

    That single owner is the point. This migration originally answered the
    question itself, matching on ``spawned_thread_id`` alone, and refused to
    migrate when more than one node matched -- but reusing one delegate thread
    across several parent tasks is ordinary, so the "ambiguity" was normal data
    and the migration aborted a production release on it. Two definitions of
    one lineage rule is exactly the condition that caused that.

    A frozen copy is right for schema *shape*: ``_CHECKPOINTS_7_DDL`` in
    ``infra.database_migrations`` is a copy for a real reason, because a
    database migrated last month must present the same table as one migrated
    today. This is not shape. It is a semantic read of existing data, and if
    CORSIA's definition of attachment changes the migration should follow it
    rather than preserve a private disagreement. A copy plus a drift test only
    detects divergence after the fact, and turns any intentional change to the
    runtime rule into a second edit somewhere else.

    What one definition does owe its frozen caller is a stated contract, so
    the rule may change while staying runnable against a v7 database. That
    contract is declared on ``attached_parent_node`` and pinned by
    ``test_attachment_resolves_against_a_v7_database``.

    A child plan with no live attachment is absent from the result: its open
    nodes then keep their own commitment, which is the same answer the runtime
    gives.

    Raises:
        PlanMigrationError: A child plan *holding open work* has more than one
            live attachment, which the runtime refuses too -- unlike the merely
            repeated attachment history that is ordinary and must migrate
            cleanly. Plans whose work has all settled are never asked, so they
            cannot abort a migration over an answer nobody reads; refusing on
            exactly that kind of unused question is what caused the outage.
    """
    from plans import public
    from plans import public

    attachments: dict[str, str] = {}
    # Only child plans holding open work are asked about, because that is the
    # only case the caller consumes: a settled node keeps its own commitment
    # regardless of what its plan hangs from. On the production database this is
    # 197 plans rather than 610, and every plan it skips resolves to an
    # attachment no caller reads -- verified by resolving both sets and
    # confirming the omitted ones hold zero open nodes.
    #
    # This is a correctness narrowing, not a speed one: the whole lineage
    # resolve measures ~0.0s either way. It matters because a question nobody
    # reads can still abort a migration, and refusing over exactly such a
    # question is what took production down.
    rows = db.execute(
        """SELECT p.id FROM plans p
           WHERE p.owner_kind='thread' AND p.parent_plan_id IS NOT NULL
             AND EXISTS (
               SELECT 1 FROM plan_nodes n
               WHERE n.plan_id = p.id
                 AND n.state IN ('pending', 'active', 'review_requested')
             )
           ORDER BY p.id"""
    ).fetchall()
    for row in rows:
        child_plan_id = str(row["id"])
        try:
            node = public.attached_parent_node(child_plan_id, db=db)
        except public.PlanError as error:
            raise PlanMigrationError(
                f"child plan {child_plan_id} cannot be resolved: {error}"
            ) from error
        if node is not None:
            attachments[child_plan_id] = str(node["id"])
    return attachments


def commitments(db: sqlite3.Connection) -> dict[str, str]:
    """Resolve self, split, accepted-follow-up, and attached-child commitments."""
    nodes = {str(row["id"]): dict(row) for row in db.execute("SELECT * FROM plan_nodes")}
    resolved = {node_id: node_id for node_id in nodes}
    inherited: dict[str, str] = {}

    for event in db.execute(
        """SELECT node_id,payload_json FROM plan_node_events
           WHERE event='node_split' ORDER BY created_at,rowid"""
    ):
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlanMigrationError("a node_split event has invalid payload") from exc
        target = payload.get("split_node_id") if isinstance(payload, dict) else None
        source = str(event["node_id"])
        if not isinstance(target, str) or target not in nodes or source not in nodes:
            raise PlanMigrationError("a node_split event cannot be resolved")
        prior = inherited.get(target)
        if prior is not None and prior != source:
            raise PlanMigrationError("a split node has multiple source commitments")
        inherited[target] = source

    for node_id, node in nodes.items():
        source = node["follow_up_of"]
        if (
            isinstance(source, str)
            and source in nodes
            and nodes[source]["state"] == "accepted_with_followup"
        ):
            inherited[node_id] = source

    child_sources = _child_attachments(db)

    for _ in range(len(nodes) + 1):
        changed = False
        for node_id, source in inherited.items():
            if resolved[node_id] != resolved[source]:
                resolved[node_id] = resolved[source]
                changed = True
        for node_id, node in nodes.items():
            source = child_sources.get(str(node["plan_id"]))
            is_open = node["state"] in {"pending", "active", "review_requested"}
            if source is not None and is_open and resolved[node_id] != resolved[source]:
                resolved[node_id] = resolved[source]
                changed = True
        if not changed:
            return resolved
    raise PlanMigrationError("commitment lineage did not converge")


