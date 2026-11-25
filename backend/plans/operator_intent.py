"""Read the displayed intent for a batch of operator nodes."""
from __future__ import annotations

import json

from plans import intent_creation
from infra.thread_title_constants import DEFAULT_THREAD_TITLE

EMPTY = {
    "intent": None, "intent_is_person_comment": False, "intent_parent_title": None,
    "intent_owner_name": None, "intent_run_title": None,
}


def read(node_ids: list[str], *, db) -> dict[str, dict]:
    """Resolve worker parents and canonical intent references in one database read."""
    if not node_ids:
        return {}
    rows = db.execute(
        f"""WITH RECURSIVE
        opened AS (
          SELECT n.id, n.workflow_step, n.parent_id,
                 CASE WHEN p.owner_kind = 'thread'
                   THEN ({intent_creation.delegated_parent_sql('p.owner_id')}) END AS delegated_from
          FROM json_each(?) wanted CROSS JOIN plan_nodes n ON n.id = wanted.value
          JOIN plans p ON p.id = n.plan_id
        ),
        intent_roots(opened_id, source_id) AS (
          SELECT opened.id, COALESCE(run.id, selected.id)
          FROM opened
          JOIN plan_nodes selected ON selected.id = COALESCE(opened.delegated_from, opened.id)
          LEFT JOIN plan_nodes run ON run.id = selected.parent_id AND selected.workflow_step IS NOT NULL
            AND run.kind = 'phase' AND run.workflow_ref IS NOT NULL
        ),
        {intent_creation.CANONICAL_CHAIN_SQL}
        SELECT opened.id, source.intent, parent.title AS parent_title,
               CASE WHEN parent.id IS NULL THEN NULL ELSE COALESCE(owner.title, ?) END AS owner_name,
               CASE WHEN parent.id IS NULL AND opened.workflow_step IS NOT NULL
                    THEN run.title ELSE NULL END AS run_title
        FROM opened
        JOIN chain ON chain.opened_id = opened.id
        JOIN plan_nodes source ON source.id = chain.source_id AND source.intent_node_id IS NULL
        LEFT JOIN plan_nodes parent ON parent.id = opened.delegated_from
        LEFT JOIN plans source_plan ON source_plan.id = source.plan_id
        LEFT JOIN threads owner ON source_plan.owner_kind = 'thread' AND owner.id = source_plan.owner_id
        LEFT JOIN plan_nodes run ON run.id = opened.parent_id AND run.kind = 'phase' AND run.workflow_ref IS NOT NULL
        """, (json.dumps(node_ids), DEFAULT_THREAD_TITLE),
    ).fetchall()
    result = {}
    for row in rows:
        intent = row["intent"]
        result[str(row["id"])] = {
            "intent": intent,
            "intent_is_person_comment": bool(intent and intent.startswith("User document feedback:")),
            "intent_parent_title": row["parent_title"],
            "intent_owner_name": row["owner_name"],
            "intent_run_title": row["run_title"],
        }
    if len(result) != len(set(node_ids)):
        raise ValueError("Cannot resolve an operator node's canonical intent")
    return result
