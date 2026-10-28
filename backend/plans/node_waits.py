"""The two things that keep open work from starting: an unmet dependency and a hold.

Each has one predicate here. The plan list, a single node's detail, the
status read, admission and the pane all read them, so a node never waits in
one read and not in another (#2213). Whether a wait reads red is a display
question ``operator_node_state.quiet`` answers from these same facts.
"""

from __future__ import annotations

from collections import defaultdict
import json
import sqlite3

from infra.packed_rows import fetch_all

#: The node states in which a dependency or a hold keeps work from starting.
WAITABLE_STATES = frozenset({"pending", "active"})


def encoded_ids(node_ids) -> str:
    """One set selector for owner reads, without per-node placeholder batches."""
    return json.dumps(list(dict.fromkeys(str(node_id) for node_id in node_ids)))


#: Deeper than any real plan; it only bounds the walk if a parent link ever loops.
_MAX_ANCESTOR_DEPTH = 64


def _prerequisite_columns(node_id: str, owner: str, compact: bool) -> str:
    """The prerequisite row's columns; ``owner`` is the alias of the waiting node."""
    if compact:
        return f"{node_id},p.id,p.state,p.plan_id,p.archived_at"
    return f"""{node_id},p.id,p.title,p.state,
               CASE WHEN p.plan_id<>{owner}.plan_id THEN pl.owner_id END AS foreign_owner,
               CASE WHEN p.plan_id<>{owner}.plan_id
                    THEN COALESCE(NULLIF(t.title,''),'Untitled thread') END AS foreign_label"""


def unmet_prerequisites(
    connection: sqlite3.Connection, node_ids, *, compact: bool = False, include_met: bool = False,
) -> dict[str, list]:
    """Each node's prerequisites that are not accepted yet, in plan order.

    A dependency is met only by an accepted prerequisite. A failed one is
    unmet and never will be met; the caller names that as its own cause.
    For a prerequisite in another plan (#1552), ``foreign_owner`` is the
    thread owning it and ``foreign_label`` that thread's title; else None.
    """
    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    found: dict[str, list] = defaultdict(list)
    if not ids:
        return {}
    unmet = "" if include_met else "AND p.state NOT IN ('accepted','accepted_with_followup')"
    # A node's own edges (depth 0), then those of its open ancestors: an
    # ancestor's dependency holds its descendants too. Nearest first, so a
    # prerequisite reached at several levels keeps the nearest one.
    touched = set()
    for row in fetch_all(
        connection,
        f"""WITH RECURSIVE up(root,id,depth) AS (
                SELECT id,parent_id,1 FROM plan_nodes
                WHERE id IN (SELECT value FROM json_each(?1)) AND parent_id IS NOT NULL
                UNION ALL
                SELECT up.root,a.parent_id,up.depth+1 FROM up JOIN plan_nodes a ON a.id=up.id
                WHERE a.parent_id IS NOT NULL AND up.depth<{_MAX_ANCESTOR_DEPTH}
            )
            SELECT {_prerequisite_columns('d.node_id', 'n', compact)},NULL AS via,p.order_key AS order_key,0 AS depth
            FROM plan_node_dependencies d
            JOIN plan_nodes p ON p.id=d.depends_on_node_id
            JOIN plan_nodes n ON n.id=d.node_id
            JOIN plans pl ON pl.id=p.plan_id
            LEFT JOIN threads t ON t.id=pl.owner_id
            WHERE d.node_id IN (SELECT value FROM json_each(?1)) {unmet}
            UNION ALL
            SELECT {_prerequisite_columns('up.root', 'r', compact)},up.id,p.order_key,up.depth
            FROM up JOIN plan_node_dependencies d ON d.node_id=up.id
            JOIN plan_nodes n ON n.id=up.id
            {"" if compact else "JOIN plan_nodes r ON r.id=up.root"}
            JOIN plan_nodes p ON p.id=d.depends_on_node_id
            JOIN plans pl ON pl.id=p.plan_id
            LEFT JOIN threads t ON t.id=pl.owner_id
            WHERE n.state IN ('pending','active','review_requested') AND n.archived_at IS NULL {unmet}
            ORDER BY 1,depth,order_key,2""",
        (encoded_ids(ids),),
    ):
        node_id = str(row["node_id"])
        if row["depth"] == 0:
            found[node_id].append(row)
        elif all(str(have["id"]) != str(row["id"]) for have in found.get(node_id, ())):
            found[node_id].append(row)
            touched.add(node_id)
    for node_id in touched:
        found[node_id].sort(key=lambda row: (row["order_key"], str(row["id"])))
    return dict(found)


def open_holds(
    connection: sqlite3.Connection, node_ids, *, compact: bool = False,
) -> dict[str, list]:
    """Each node's uncleared holds: a pause first, then the oldest first."""
    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    found: dict[str, list] = defaultdict(list)
    if ids:
        columns = "node_id,kind,owner_kind,reason_key" if compact else "*"
        for row in fetch_all(
            connection,
            f"""SELECT {columns} FROM plan_node_holds
                WHERE node_id IN (SELECT value FROM json_each(?)) AND cleared_at IS NULL
                ORDER BY node_id,CASE kind WHEN 'paused' THEN 0 ELSE 1 END,created_at,id""",
            (encoded_ids(ids),),
        ):
            found[str(row["node_id"])].append(row)
    return dict(found)


def armed_watches(
    connection: sqlite3.Connection, node_ids, *, compact: bool = False,
) -> dict[str, list]:
    """Each node's armed watches, oldest first, with the blocker each one is.

    A blocker someone other than the user clears is armed as a watch
    (``plans.blocked_wait``), and its ``watch_armed`` event is the one record
    of who clears it and why: ``blocked_owner_kind``, ``blocked_owner_id`` and
    ``blocked_reason`` come from there, and are None for any other watch. A
    scheduled task (``plans.scheduled_wait``) carries ``scheduled_reason`` the
    same way.
    """
    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    found: dict[str, list] = defaultdict(list)
    if ids:
        columns = """w.id,w.plan_node_id,w.condition_type,w.condition_value,
                     json_extract(e.payload_json,'$.blocked.owner_kind') AS blocked_owner_kind"""
        if not compact:
            columns += """,w.armed_at,w.expected_event,w.artifact_integration,
                       json_extract(e.payload_json,'$.blocked.owner_id') AS blocked_owner_id,
                       json_extract(e.payload_json,'$.blocked.reason') AS blocked_reason,
                       json_extract(e.payload_json,'$.scheduled.reason') AS scheduled_reason,
                       i.id AS artifact_id,i.title AS artifact_title"""
        for row in fetch_all(
            connection,
            f"""SELECT {columns}
                FROM expected_event_watches w
                LEFT JOIN plan_node_events e
                  ON e.node_id=w.plan_node_id AND e.event='watch_armed'
                 AND json_extract(e.payload_json,'$.watch_id')=w.id
                LEFT JOIN artifact_identities i
                  ON i.integration=w.artifact_integration AND i.type=w.artifact_type
                 AND i.external_id=w.artifact_external_id
                WHERE w.plan_node_id IN (SELECT value FROM json_each(?)) AND w.status='armed'
                ORDER BY w.plan_node_id,w.armed_at,w.id""",
            (encoded_ids(ids),),
        ):
            found[str(row["plan_node_id"])].append(row)
    return dict(found)


def armed_in_plans(connection: sqlite3.Connection, plan_ids) -> dict[str, list[tuple]]:
    """Each listed plan's armed watches (``armed_watches`` rows), each with its task's title, in two reads.

    For a parent's child summaries, whose rollups list no nodes. The read is
    driven from the few armed watches, never from a plan's node history.
    """
    ids = list(dict.fromkeys(str(plan_id) for plan_id in plan_ids))
    if not ids:
        return {}
    node_of = {
        str(row["plan_node_id"]): (str(row["plan_id"]), str(row["title"]))
        for row in fetch_all(
            connection,
            """SELECT w.plan_node_id,n.plan_id,n.title
                FROM expected_event_watches w CROSS JOIN plan_nodes n ON n.id=w.plan_node_id
                WHERE w.status='armed' AND n.plan_id IN (SELECT value FROM json_each(?))""",
            (encoded_ids(ids),),
        )
    }
    found: dict[str, list[tuple]] = defaultdict(list)
    for node_id, rows in armed_watches(connection, node_of).items():
        plan_id, title = node_of[node_id]
        found[plan_id].extend((row, title) for row in rows)
    return dict(found)


def dependency_cause(row) -> dict:
    """The lifecycle part of a prerequisite cause, shared by narrow/full reads."""
    return {
        "kind": "blocked",
        "reason_key": "dependency_failed" if row["state"] == "failed" else "dependency",
        "dependency_node_id": row["id"],
    }


def watch_cause(row) -> dict:
    """The lifecycle part of an armed watch, independent of presentation prose."""
    return {
        "kind": "waiting", "reason_key": row["condition_type"], "watch_id": row["id"],
        "blocked_wait": row["blocked_owner_kind"] is not None,
        "ends_at": row["condition_value"] if row["condition_type"] == "deadline" else None,
    }


def named_watch_cause(row) -> dict:
    """A full-read watch's cause with what it waits on: why, and the watched artifact.

    Only the full ``armed_watches`` read selects these columns; the compact read
    keeps ``watch_cause`` alone, so the narrow and full lifecycle parts agree.
    """
    return {
        **watch_cause(row),
        "blocked_reason": row["blocked_reason"],
        "scheduled_reason": row["scheduled_reason"],
        "artifact_id": row["artifact_id"],
        "artifact_title": row["artifact_title"],
    }


def held_by(holds) -> str | None:
    """Who holds a node: the owner kind of its oldest placed block, else None."""
    return next((hold["owner_kind"] for hold in holds if hold["kind"] == "blocked"), None)


def project(connection: sqlite3.Connection, rows) -> dict[str, tuple[list[str], str | None]]:
    """Each node's unmet prerequisite ids and its holder, empty unless it is open."""
    open_ids = [str(row["id"]) for row in rows if row["state"] in WAITABLE_STATES]
    prerequisites = unmet_prerequisites(connection, open_ids)
    holds = open_holds(connection, open_ids)
    return {
        str(row["id"]): (
            [str(item["id"]) for item in prerequisites.get(str(row["id"]), [])],
            held_by(holds.get(str(row["id"]), [])),
        )
        for row in rows
    }


def waits_on_dependency(connection: sqlite3.Connection, node_id: str, state: str) -> bool:
    """Whether an open node has an unmet dependency: the writer-side delta test."""
    return state in WAITABLE_STATES and bool(unmet_prerequisites(connection, [node_id]))
