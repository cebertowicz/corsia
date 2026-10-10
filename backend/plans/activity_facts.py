"""Minimal scope node reads and orchestration of canonical owner fact adapters."""

from __future__ import annotations

from collections import defaultdict
import json

from infra.packed_rows import fetch_all
from plans import (
    admission_attempts, admission_projection, artifact_links, decision_projection, delegation,
    hierarchy,
    node_states, node_waits, not_moving, operator_display_state, operator_stopped, rollup,
    user_steering, wait_causes,
)


_COMPACT_NODE_COLUMNS = """n.id,n.plan_id,n.parent_id,n.order_key,n.kind,n.state,
    n.assignee,n.spawned_thread_id,n.executor_thread_id,n.owner_thread_id,
    n.requirements_revision,n.updated_at,p.owner_kind,p.owner_id"""


def _apply_owner_facts(by_plan: dict[str, list[dict]], *, db) -> None:
    nodes = [row for rows in by_plan.values() for row in rows]
    ids = [str(row["id"]) for row in nodes]
    holds = node_waits.open_holds(db, ids, compact=True)
    dependencies = node_waits.unmet_prerequisites(db, ids, compact=True)
    watches = node_waits.armed_watches(db, ids, compact=True)
    attempts = admission_attempts.open_attempts(ids, db=db, compact=True)
    attention = artifact_links.raised_attention_nodes(ids, db=db)
    for row in nodes:
        node_id = str(row["id"])
        placed = [dict(hold) | {"placed": True} for hold in holds.get(node_id, [])]
        prerequisites = dependencies.get(node_id, [])
        armed = [node_waits.watch_cause(watch) for watch in watches.get(node_id, [])]
        causes = [*placed, *(node_waits.dependency_cause(dep) for dep in prerequisites), *armed]
        pause = user_steering.placed_pause(placed)
        deadlines = not_moving.deadline_range(armed)
        attempt = attempts.get(node_id)
        if attempt is not None and admission_projection.superseded_by(attempt, row, row) is not None:
            attempt = None
        row.update({
            "has_user_block": not_moving.user_blocker(placed),
            "has_user_pause": pause is not None and user_steering.is_user_pause(pause),
            "has_attention": node_id in attention,
            "wait_deadline": deadlines[0] if deadlines else None,
            "wait_deadline_latest": deadlines[1] if deadlines else None,
            "attempt_id": attempt["id"] if attempt is not None else None,
            "causes": causes,
            # Liveness asks about unarchived membership of the node's OWN plan,
            # including suppressed sent-back rows. Display quietness is distinct.
            "own_prerequisite_ids": {
                str(dep["id"]) for dep in prerequisites
                if dep["plan_id"] == row["plan_id"] and dep["archived_at"] is None
            },
        })


def compact_nodes(plan_ids: list[str], *, db) -> dict[str, list[dict]]:
    """Read minimal nodes; every lifecycle fact comes from its canonical owner."""
    by_plan: dict[str, list[dict]] = defaultdict(list)
    for start in range(0, len(plan_ids), hierarchy._ID_CHUNK):
        chunk = plan_ids[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            db,
            f"""{rollup.rejected_branch_cte(marks)}
                SELECT {_COMPACT_NODE_COLUMNS} FROM plan_nodes n
                JOIN plans p ON p.id=n.plan_id
                WHERE n.plan_id IN ({marks}) AND {node_states.unarchived_sql('n')}
                  AND NOT EXISTS (SELECT 1 FROM rejected WHERE rejected.id=n.id)
                ORDER BY n.plan_id,n.order_key,n.id""",
            (*chunk, *chunk),
        ):
            by_plan[str(row["plan_id"])].append(dict(row))
    _apply_owner_facts(by_plan, db=db)
    return dict(by_plan)


def delegation_closure_ids(seed_ids: list[str], plans: dict[str, dict], *, db) -> list[str]:
    """The seeds plus every plan delegated from them, whose state can reach a seed's row.

    A plan's state is consumed only by the shown plan itself or, as a child
    cause, by the open step that delegated to it. Every other plan in the tree
    is neither shown nor read by a parent, so it is never fetched or reduced.
    One recursive read follows the delegating steps; it ignores the
    newest-attachment rule, so it may name a plan the exact facts leave out and
    never misses one.
    """
    live, states = delegation._live_attachment(alias="n")
    found = json.loads(db.execute(
        f"""WITH RECURSIVE reach(id) AS (
              SELECT value FROM json_each(?)
              UNION
              SELECT c.id FROM reach
                CROSS JOIN plan_nodes n ON n.plan_id=reach.id
                CROSS JOIN plans c ON {hierarchy.delegation_edge_sql('n', 'c')}
               WHERE {live}
            )
            SELECT json_group_array(id) FROM reach""",
        (json.dumps(seed_ids), *states),
    ).fetchone()[0])
    return [plan_id for plan_id in found if plan_id in plans]


def depths(plans: list) -> dict[str, int]:
    """Index descendant plans by their depth under the requested scope."""
    by_id = {str(plan["id"]): plan for plan in plans}
    answer: dict[str, int] = {}
    for plan_id in by_id:
        chain: list[str] = []
        cursor: str | None = plan_id
        while cursor in by_id and cursor not in answer and cursor not in chain:
            chain.append(cursor)
            parent = by_id[cursor]["parent_plan_id"]
            cursor = str(parent) if parent is not None else None
        depth = answer.get(cursor, -1) + 1 if cursor is not None else 0
        for item in reversed(chain):
            answer[item] = depth
            depth += 1
    return answer


def decision_indexes(plan_ids: list[str], *, db) -> tuple[list[dict], set[str], set[str]]:
    """Return compact decision facts, human targets, and canonical decision waits."""
    facts = decision_projection.activity_facts(plan_ids, db=db)
    human_decisions = {str(item["node_id"]) for item in facts if item["answerable"]}
    waits = {
        str(item["source_node_id"])
        for item in facts
        if wait_causes.decision_wait_kind(
            item["decision_state"], author=item["owner_id"], reviewer=item["spawned_by"],
            answerable=item["answerable"],
        ) is not None
    }
    return facts, human_decisions, waits


def executor_stop_facts(
    nodes_by_plan: dict[str, list[dict]], plans: dict[str, dict], *, db,
) -> dict[str, dict]:
    """Return canonical stop facts for compact nodes with valid selections."""
    selected: list[tuple[dict, str]] = []
    executor_ids: set[str] = set()
    for plan_id, rows in nodes_by_plan.items():
        plan = plans[plan_id]
        for row in rows:
            if row["attempt_id"] is None:
                continue
            executor = admission_projection.executor_of(row, plan)
            if executor is None:
                continue
            executor_ids.add(executor)
            selected.append((row, executor))
    facts = operator_stopped.executor_facts(executor_ids, db=db)
    return {
        str(row["id"]): fact
        for row, executor in selected
        if (fact := facts.get(executor)) is not None
    }


def review_kind(row: dict, plan: dict, human_decisions: set[str]) -> str | None:
    """Classify the canonical reviewer using compact facts only."""
    return operator_display_state.review_kind_from_facts(
        row["state"], row["assignee"], plan["owner_kind"],
        human_decision=str(row["id"]) in human_decisions,
    )
