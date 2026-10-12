"""Batched readers for the per-node facts the operator display projects.

Each reader answers for a whole batch of plans with one query, so the operator
projection's query count stays bounded by the kinds of fact, not by the number
of nodes. ``operator_node_state`` composes them; nothing here decides display.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from infra.packed_rows import fetch_all
from plans import closure, commitment_cost, node_cost, node_cost_scoped, node_states, review_evidence
from plans.commitment_cost import FAILED_RELEASES

_ID_CHUNK = 400


def executor_labels(ids: set[str], *, db) -> dict[str, str]:
    """Return each thread's display title, keyed by thread id."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT id,title FROM threads WHERE id IN ({marks})",
        tuple(sorted(ids)),
    ).fetchall()
    return {
        str(row["id"]): str(row["title"] or "Untitled thread")
        for row in rows
    }


def project_costs(
    node_plans, *, db, now: datetime, wanted: bool = True, detail_ids: frozenset[str] | None = None,
) -> tuple[dict, dict, dict]:
    """Return commitment totals, per-plan node costs and initiative totals on one clock.

    Unwanted, every commitment and node maps to None and nothing is summed: the
    roll-up reads the usage ledger, too slow to run under the writer lock (#2489).

    ``detail_ids`` (a read of the open work, not the plan) costs each listed node its own
    cost alone and rolls up, with its commitment and initiative totals, only the nodes it
    names; every other node's rolled, commitment and initiative figures are None.
    """
    if wanted and detail_ids is not None:
        return _scoped_costs(node_plans, db=db, now=now, detail_ids=detail_ids)
    if not wanted:
        nodes: dict[str, dict] = defaultdict(dict)
        for node, plan in node_plans:
            nodes[str(plan["id"])][node["id"]] = None
        return (
            {str(node["commitment_id"]): None for node, _plan in node_plans}, dict(nodes),
            {str(node["id"]): None for node, _plan in node_plans},
        )
    costs = commitment_cost.project_many(
        {str(node["commitment_id"]) for node, _plan in node_plans}, db=db, now=now,
    )
    node_costs, initiatives = node_cost.project_with_initiatives(
        {str(plan["id"]) for _node, plan in node_plans}, db=db, now=now,
    )
    return costs, node_costs, initiatives


def _scoped_costs(node_plans, *, db, now: datetime, detail_ids: frozenset[str]) -> tuple[dict, dict, dict]:
    listed = [str(node["id"]) for node, _plan in node_plans]
    known = node_cost_scoped.own_timed(listed, db=db, now=now)
    own = node_cost_scoped.own_listed(listed, db=db, now=now, known=known)
    wanted = [node_id for node_id in listed if node_id in detail_ids]
    rolled = node_cost_scoped.rolled_many(wanted, db=db, now=now, known=known)
    nodes: dict[str, dict] = defaultdict(dict)
    for node, plan in node_plans:
        node_id = str(node["id"])
        nodes[str(plan["id"])][node_id] = {"own": own[node_id], "rolled": rolled.get(node_id)}
    roots = [
        str(node["id"]) for node, _plan in node_plans
        if str(node["id"]) in detail_ids and str(node["commitment_id"]) == str(node["id"])
    ]
    commitments = commitment_cost.project_many(roots, db=db, now=now) if roots else {}
    initiatives: dict[str, dict | None] = {str(node["id"]): None for node, _plan in node_plans}
    initiatives.update(node_cost_scoped.initiatives_of(wanted, db=db, now=now, known=known))
    return commitments, dict(nodes), initiatives


#: What ``detail`` returns for a reader that shows none of it (no executor labels).
NO_DETAIL: tuple = (None, {}, {}, {}, {}, {})


def scope(
    plan_column: str, node_column: str, plan_ids: list[str], chunk: list[str] | None,
) -> tuple[str, tuple]:
    """The ``WHERE`` condition and parameters naming the plans' nodes, or just these ``chunk`` ids.

    Ids already belong to the plans, so a chunk replaces the plan condition: the read then
    finds each node by its own key and never walks the plan's rows to filter them.
    """
    if chunk is None:
        return f"{plan_column} IN ({','.join('?' for _ in plan_ids)})", tuple(plan_ids)
    return f"{node_column} IN ({','.join('?' for _ in chunk)})", tuple(chunk)


def id_chunks(ids: list[str] | None) -> list[list[str] | None]:
    """``ids`` in bounded ``IN`` lists; ``[None]`` (one unrestricted pass) when no ids restrict."""
    if ids is None:
        return [None]
    return [ids[start:start + _ID_CHUNK] for start in range(0, len(ids), _ID_CHUNK)]


def detail(
    projected, node_plans, executor_by_node, *, limit: int, db, scoped: bool = False,
) -> tuple:
    """Executor labels, attempt history and counts, review evidence, last changes, settled reasons.

    ``scoped`` reads each fact for the projected nodes alone, not for every node of their
    plans: the projection is then the open work of a plan and the cost follows it. A
    container's last change is then its own latest event, not its work's.
    """
    plan_ids = [str(item.plan["id"]) for item in projected]
    node_ids = sorted({str(node["id"]) for node, _plan in node_plans}) if scoped else None
    labels = executor_labels({v for v in executor_by_node.values() if v is not None}, db=db)
    history, counts = attempts(plan_ids, labels, limit=limit, db=db, node_ids=node_ids)
    all_nodes = [list(item.nodes) for item in projected]
    unlisted = (
        folded_changes(db, sorted(node_states.parent_ids(node for nodes in all_nodes for node in nodes)))
        if scoped else None
    )
    changed = last_changed_all(
        all_nodes, last_transitions(plan_ids, db=db, node_ids=node_ids), unlisted,
    )
    return (
        labels, history, counts,
        review_evidence.latest_by_node(plan_ids, db=db, node_ids=node_ids), changed,
        closure.settled_reasons(db, [node for node, _plan in node_plans], labels=labels),
    )


_ATTEMPTS_SQL = """WITH ordered AS (
      SELECT *,
             ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY selected_at DESC,id DESC) AS history_rank,
             COUNT(*) OVER (PARTITION BY node_id) AS history_count
      FROM plan_node_attempts
      WHERE {where}
    ), ranked AS (
      SELECT *,
             SUM(history_rank>1 AND release_reason IN ({failure_marks})) OVER (PARTITION BY node_id)
               AS friction_count
      FROM ordered
    )
    SELECT * FROM ranked WHERE history_rank<=?
    ORDER BY node_id,attempt_ordinal"""


def attempts(
    plan_ids: list[str],
    labels: dict[str, str],
    *,
    limit: int,
    db,
    node_ids: list[str] | None = None,
) -> tuple[dict[str, list[dict]], dict[str, dict[str, int]]]:
    """Return each node's latest ``limit`` attempts and its whole-history counts.

    The counts are ``attempt_count``, every attempt, and ``friction_count``, the
    retries a failed try forced. A retry takes one of two shapes, and both
    count: an attempt released for a failure reason
    (``commitment_cost.FAILED_RELEASES``) that a later attempt followed, and a
    rejection, whose retry is a fresh revision node linked to the rejected one
    (see ``revisions``). A node that failed on its last try shows that failure,
    not a retry.

    ``labels`` is extended in place with any historical executor it lacks, so
    an attempt names the thread that ran it even after the plan moved on.
    """
    if not plan_ids:
        return {}, {}
    failures = sorted(FAILED_RELEASES)
    failure_marks = ",".join("?" for _ in failures)
    rows = []
    for chunk in id_chunks(node_ids):
        where, params = scope("plan_id", "node_id", plan_ids, chunk)
        rows.extend(fetch_all(
            db,
            _ATTEMPTS_SQL.format(where=where, failure_marks=failure_marks),
            (*params, *failures, limit),
        ))
    historical_ids = {str(row["executor_thread_id"]) for row in rows}
    labels.update(
        executor_labels(historical_ids - set(labels), db=db)
    )
    indexed: dict[str, list[dict]] = defaultdict(list)
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        executor_id = str(row["executor_thread_id"])
        counts[str(row["node_id"])] = {
            "attempt_count": int(row["history_count"]),
            "friction_count": int(row["friction_count"]),
            "revision_round": 0,
        }
        indexed[str(row["node_id"])].append(
            {
                "id": row["id"],
                "ordinal": row["attempt_ordinal"],
                "executor": {
                    "thread_id": executor_id,
                    "label": labels.get(executor_id, "Unavailable executor"),
                },
                "requirements_revision": row["requirements_revision"],
                "selected_at": row["selected_at"],
                "released_at": row["released_at"],
                "release_reason": row["release_reason"],
            }
        )
    for node_id, depth in revisions(plan_ids, db=db, node_ids=node_ids).items():
        counts.setdefault(
            node_id, {"attempt_count": 0, "friction_count": 0, "revision_round": 0},
        )
        counts[node_id]["friction_count"] += depth
        counts[node_id]["revision_round"] = depth
    return dict(indexed), counts


def revisions(plan_ids: list[str], *, db, node_ids: list[str] | None = None) -> dict[str, int]:
    """Return how many rejections each node is the revision of.

    A rejection is terminal for the node it lands on: the retry is a new node
    linked to it by ``follow_up_of`` and marked by its own ``revision_created``
    event, which is what separates a rejection's retry from an ordinary
    follow-up. Walking that chain is the only way the retry a rejection forced
    is visible on the node now carrying the work.
    """
    if not plan_ids:
        return {}
    found: dict[str, int] = {}
    for chunk in id_chunks(node_ids):
        found.update(_revisions(db, *scope("n.plan_id", "n.id", plan_ids, chunk)))
    return found


def _revisions(db, where: str, params: tuple) -> dict[str, int]:
    rows = db.execute(
        f"""WITH RECURSIVE revision AS (
              SELECT n.id AS node_id,n.follow_up_of AS ancestor
              FROM plan_nodes n
              JOIN plan_node_events e
                ON e.node_id=n.id AND e.event='revision_created'
              WHERE {where} AND n.follow_up_of IS NOT NULL
              UNION ALL
              SELECT r.node_id,p.follow_up_of
              FROM revision r
              JOIN plan_nodes p ON p.id=r.ancestor
              JOIN plan_node_events e
                ON e.node_id=p.id AND e.event='revision_created'
              WHERE p.follow_up_of IS NOT NULL
            )
            SELECT node_id,COUNT(*) AS depth FROM revision GROUP BY node_id""",
        params,
    ).fetchall()
    return {str(row["node_id"]): int(row["depth"]) for row in rows}


def revision_root(node_id: str, *, db) -> str:
    """Return the submission one node's rejection chain began with.

    Walks the ``follow_up_of`` + ``revision_created`` lineage ``revisions``
    counts, in one query, back to the one node at its root: the first
    submission a rejection ever forced a revision from. That root, unlike
    ``commitment_id``, names this one step and no other -- a delegated
    worker's top-level nodes all share one commitment with the step the
    parent delegated. A node with no such ancestry is its own root.
    """
    row = db.execute(
        """WITH RECURSIVE chain(id,depth) AS (
             SELECT ?,0
             UNION ALL
             SELECT n.follow_up_of,c.depth+1
             FROM chain c
             JOIN plan_nodes n ON n.id=c.id
             JOIN plan_node_events e
               ON e.node_id=n.id AND e.event='revision_created'
             WHERE n.follow_up_of IS NOT NULL
           )
           SELECT id FROM chain ORDER BY depth DESC LIMIT 1""",
        (node_id,),
    ).fetchone()
    return str(row["id"])


def last_transitions(
    plan_ids: list[str], *, db, node_ids: list[str] | None = None,
) -> dict[str, str]:
    """Return when each node last recorded an event (of ``node_ids`` alone when given)."""
    if not plan_ids:
        return {}
    found: dict[str, str] = {}
    for chunk in id_chunks(node_ids):
        where, params = scope("n.plan_id", "e.node_id", plan_ids, chunk)
        rows = fetch_all(
            db,
            f"""SELECT e.node_id,MAX(e.created_at) AS created_at
                FROM plan_node_events e
                JOIN plan_nodes n ON n.id=e.node_id
                WHERE {where}
                GROUP BY e.node_id""",
            params,
        )
        found.update({str(row["node_id"]): str(row["created_at"]) for row in rows})
    return found


def last_changed(node, transitions: dict[str, str]) -> str:
    """When one node last changed: its latest event, else its own row's update.

    The one rule behind a node's ``last_transition_at`` and the settle time the
    fold reads, so the pane and the agent's full read cannot disagree.
    """
    return transitions.get(str(node["id"]), node["updated_at"])


def folded_changes(db, container_ids: list[str]) -> dict[str, str]:
    """When each container's latest folded child changed, for the children a read leaves unlisted.

    One index walk per container (``idx_plan_nodes_last_changed``, newest first, stopping
    at the first folded child), so it costs the container's open children, not its history.
    A folded child that is itself a container is dated by when it folded, which is when its
    last open work settled.
    """
    found: dict[str, str] = {}
    for container_id in container_ids:
        row = db.execute(
            "SELECT own_last_changed,folded_at,child_in_work FROM plan_nodes "
            f"WHERE parent_id=? AND {node_states.unarchived_sql('plan_nodes')} AND folded=1 "
            "ORDER BY own_last_changed DESC LIMIT 1",
            (container_id,),
        ).fetchone()
        if row is not None:
            stamp = str(row["own_last_changed"])
            if row["child_in_work"] > 0 and row["folded_at"]:
                stamp = max(stamp, str(row["folded_at"]))
            found[container_id] = stamp
    return found


def last_changed_all(
    plans: list[list], transitions: dict[str, str], unlisted: dict[str, str] | None = None,
) -> dict[str, str]:
    """``last_changed`` for each plan's nodes, a container dated by its work.

    ``unlisted`` (a read of the open work) names each container's latest change among the
    children the read did not load.
    """
    answer: dict[str, str] = {}
    for nodes in plans:
        answer.update(_plan_last_changed(nodes, transitions, unlisted))
    return answer


def _plan_last_changed(
    nodes: list, transitions: dict[str, str], unlisted: dict[str, str] | None = None,
) -> dict[str, str]:
    """``last_changed`` for one plan's nodes, a container dated by its work.

    A container (``node_states.is_container``) has no work of its own and
    reads as its leaves, so it changed when its latest unarchived child did,
    a closed one included: closing settles it. Its own title or criteria edits
    never date it (a finished wave read "yesterday" when its last child
    closed today).
    """
    by_id = {str(node["id"]): node for node in nodes}
    with_children = node_states.parent_ids(nodes)
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        archived = node["archived_at"] if "archived_at" in node.keys() else None
        if node["parent_id"] is not None and archived is None:
            children[str(node["parent_id"])].append(str(node["id"]))
    answer: dict[str, str] = {}

    def changed(node_id: str, seen: frozenset[str]) -> str:
        if node_id not in answer:
            node = by_id[node_id]
            kids = [kid for kid in children[node_id] if kid not in seen]
            folded = (unlisted or {}).get(node_id)
            if (kids or folded) and node_states.is_container(node, node_id in with_children):
                answer[node_id] = max(
                    [changed(kid, seen | {node_id}) for kid in kids] + ([folded] if folded else []),
                )
            else:
                answer[node_id] = last_changed(node, transitions)
        return answer[node_id]

    for node_id in by_id:
        changed(node_id, frozenset())
    return answer
