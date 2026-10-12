"""How long each node waited on something an agent owns: derived on read.

Waiting time is wall clock, never spend: it sits beside ``execution`` in
``node_cost`` and is never added to tokens or model cost. A node waits while it
is open and at least one of these holds:

- an armed watch (every plan watch is agent-, external-, event- or
  deadline-owned: a blocker only the user clears stays a hold, never a watch,
  ``plans.blocked_wait``);
- an unmet dependency: an edge present while its prerequisite is not accepted;
- ``review_requested`` while the node links a registered GitHub pull request.

User-owned holds (the user's pause, a blocker on the user) and whole-plan
pauses are the user's time, so they are cut out; so is anything after the
node settles. Causes that
overlap count once. An interval still open runs to ``now``.

Nothing is stored. Intervals are rebuilt from the append-only
``plan_node_events`` (state transitions, dependency added/removed), the plan's
own ``plan_events`` state transitions (its pauses), and each
watch's, hold's and artifact link's own timestamps -- a watch resolved by
worker retirement or expired by the continuation runner does not always leave
a node event carrying its id, so the watch row is its interval's record. Every
query is one chunked ``IN (...)`` over the requested node, plan or prerequisite
ids, so the query count is fixed per call, never per node.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Callable

from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
from plans.node_states import _ACCEPTED_STATES, _TERMINAL_STATES
from plans.status_time import parse

Interval = tuple[float, float]


def union(intervals: list[Interval]) -> list[Interval]:
    """Sorted, non-overlapping intervals covering the same time; empty ones drop."""
    merged: list[Interval] = []
    for start, end in sorted(item for item in intervals if item[1] > item[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def intersect(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """The time covered by both sides."""
    return union([
        (max(a, c), min(b, d)) for a, b in union(left) for c, d in union(right)
        if min(b, d) > max(a, c)
    ])


def subtract(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """The time in ``left`` that ``right`` does not cover."""
    result: list[Interval] = []
    cuts = union(right)
    for start, end in union(left):
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, cut_start))
            cursor = max(cursor, cut_end)
        if cursor < end:
            result.append((cursor, end))
    return result


def _at(value) -> float | None:
    parsed = parse(value) if value else None
    return parsed.timestamp() if parsed else None


def _state_spans(node: dict, transitions: list, now: float) -> list[tuple[str, float, float]]:
    """``(state, start, end)`` for each stretch of a node's life, oldest first."""
    start = _at(node["created_at"])
    if start is None:
        return []
    state = str(transitions[0]["prior_state"] or "pending") if transitions else str(node["state"])
    spans = []
    for row in transitions:
        at = _at(row["created_at"])
        if at is None:
            continue
        spans.append((state, start, at))
        state, start = str(row["new_state"]), at
    spans.append((str(node["state"]), start, now))
    return spans


def _spans_where(spans, keep) -> list[Interval]:
    return union([(start, end) for state, start, end in spans if keep(state)])


def _edge_intervals(events: list, live: list, now: float) -> dict[str, list[Interval]]:
    """Each prerequisite's edge-present intervals, from added/removed events and live rows."""
    opened: dict[str, float] = {}
    found: dict[str, list[Interval]] = defaultdict(list)
    for row in events:
        prerequisite, at = row["depends_on"], _at(row["created_at"])
        if prerequisite is None or at is None:
            continue
        if row["event"] == "dependency_added":
            opened.setdefault(str(prerequisite), at)
        elif str(prerequisite) in opened:
            found[str(prerequisite)].append((opened.pop(str(prerequisite)), at))
    for row in live:
        prerequisite = str(row["depends_on_node_id"])
        start = opened.pop(prerequisite, None) or _at(row["created_at"])
        if start is not None:
            found[prerequisite].append((start, now))
    return found


def _read(connection, ids: list[str], rows_in: Callable) -> dict[str, dict]:
    """Every fact the intervals need, one chunked query per kind."""
    facts: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for row in rows_in(connection, "SELECT id,plan_id,created_at,state FROM plan_nodes "
                       "WHERE id IN ({marks})", ids):
        facts[str(row["id"])]["node"] = dict(row)
    for row in rows_in(
        connection,
        """SELECT node_id,event,prior_state,new_state,created_at,
                  CASE WHEN event<>'state_transition' AND json_valid(payload_json)
                       THEN json_extract(payload_json,'$.depends_on_node_id') END AS depends_on
           FROM plan_node_events
           WHERE node_id IN ({marks})
             AND event IN ('state_transition','dependency_added','dependency_removed')
           ORDER BY created_at,rowid""",
        ids,
    ):
        kind = "transitions" if row["event"] == "state_transition" else "edges"
        facts[str(row["node_id"])][kind].append(row)
    queries = (
        ("live", "SELECT node_id,depends_on_node_id,created_at FROM plan_node_dependencies "
                 "WHERE node_id IN ({marks})"),
        ("watches", "SELECT plan_node_id AS node_id,status,armed_at,"
                    "COALESCE(resolved_at,expired_at,CASE WHEN status<>'armed' THEN updated_at END)"
                    " AS ended_at FROM expected_event_watches WHERE plan_node_id IN ({marks})"),
        ("holds", "SELECT node_id,created_at,cleared_at FROM plan_node_holds "
                  "WHERE owner_kind='user' AND node_id IN ({marks})"),
    )
    for key, sql in queries:
        for row in rows_in(connection, sql, ids):
            facts[str(row["node_id"])][key].append(row)
    # CROSS JOIN keeps the node list driving: with a wide id list the planner
    # otherwise walks every GitHub PR identity and probes the list per PR.
    for row in rows_in(
        connection,
        """SELECT l.node_id,MIN(l.created_at) AS linked_at
           FROM plan_node_artifact_links l CROSS JOIN artifact_identities a ON a.id=l.artifact_id
           WHERE a.integration=? AND a.type=? AND l.node_id IN ({marks})
           GROUP BY l.node_id""",
        ids, (GITHUB_INTEGRATION, PR_TYPE),
    ):
        facts[str(row["node_id"])]["pr_linked_at"] = row["linked_at"]
    return facts


def _prerequisite_unmet(connection, prerequisites: list[str], rows_in, now: float):
    """Each prerequisite's not-yet-accepted intervals, in one chunked query."""
    by_id: dict[str, dict] = {}
    for row in rows_in(
        connection,
        """SELECT n.id,n.created_at,n.state,e.prior_state,e.new_state,e.created_at AS at
           FROM plan_nodes n
           LEFT JOIN plan_node_events e ON e.node_id=n.id AND e.event='state_transition'
           WHERE n.id IN ({marks}) ORDER BY e.created_at,e.rowid""",
        prerequisites,
    ):
        entry = by_id.setdefault(str(row["id"]), {"node": dict(row), "transitions": []})
        if row["at"] is not None:
            entry["transitions"].append({**dict(row), "created_at": row["at"]})
    return {
        node_id: _spans_where(
            _state_spans(entry["node"], entry["transitions"], now),
            lambda state: state not in _ACCEPTED_STATES,
        )
        for node_id, entry in by_id.items()
    }


def _plan_paused(connection, plan_ids: list[str], rows_in: Callable, now: float):
    """Each plan's whole-plan pause stretches, from its own state transitions.

    A whole-plan pause is the user's pause (``lifecycle`` records every plan
    state change in ``plan_events``), so it is cut out like a user hold. A
    pause still in force runs to ``now``.
    """
    opened: dict[str, float] = {}
    found: dict[str, list[Interval]] = defaultdict(list)
    for row in rows_in(
        connection,
        """SELECT plan_id,prior_state,new_state,created_at FROM plan_events
           WHERE plan_id IN ({marks}) AND event='state_transition'
           ORDER BY created_at,rowid""",
        plan_ids,
    ):
        plan_id, at = str(row["plan_id"]), _at(row["created_at"])
        if at is None:
            continue
        if row["new_state"] == "paused":
            opened.setdefault(plan_id, at)
        elif plan_id in opened:
            found[plan_id].append((opened.pop(plan_id), at))
    for plan_id, start in opened.items():
        found[plan_id].append((start, now))
    return found


def own_waiting(connection, node_ids: list[str], now: datetime, rows_in: Callable) -> dict[str, int]:
    """Each node's own waiting seconds; absent nodes read 0."""
    return {
        node_id: int(sum(end - start for start, end in waited))
        for node_id, waited in own_waiting_intervals(connection, node_ids, now, rows_in).items()
    }


def own_waiting_intervals(
    connection, node_ids: list[str], now: datetime, rows_in: Callable,
) -> dict[str, list[Interval]]:
    """Each node's own merged waiting intervals (epoch seconds); the one builder.

    ``own_waiting`` is their summed length; a roll-up carries the intervals
    themselves so time that several steps waited through together counts once.
    """
    clock = now.timestamp()
    facts = _read(connection, node_ids, rows_in)
    paused = _plan_paused(
        connection,
        sorted({str(fact["node"]["plan_id"]) for fact in facts.values() if "node" in fact}),
        rows_in, clock,
    )
    prerequisites = sorted({
        str(row["depends_on"]) for fact in facts.values() for row in fact["edges"]
        if row["depends_on"] is not None
    } | {str(row["depends_on_node_id"]) for fact in facts.values() for row in fact["live"]})
    unmet = _prerequisite_unmet(connection, prerequisites, rows_in, clock)
    result: dict[str, list[Interval]] = {}
    for node_id, fact in facts.items():
        if "node" not in fact:
            continue
        spans = _state_spans(fact["node"], fact["transitions"], clock)
        causes = [
            (start, clock if row["status"] == "armed" else (_at(row["ended_at"]) or start))
            for row in fact["watches"] if (start := _at(row["armed_at"])) is not None
        ]
        for prerequisite, present in _edge_intervals(fact["edges"], fact["live"], clock).items():
            causes += intersect(present, unmet.get(prerequisite, []))
        linked = _at(fact.get("pr_linked_at"))
        if linked is not None:
            review = _spans_where(spans, lambda state: state == "review_requested")
            causes += intersect(review, [(linked, clock)])
        user_time = [
            (start, _at(row["cleared_at"]) or clock)
            for row in fact["holds"] if (start := _at(row["created_at"])) is not None
        ] + paused.get(str(fact["node"]["plan_id"]), [])
        waited = subtract(
            intersect(causes, _spans_where(spans, lambda state: state not in _TERMINAL_STATES)),
            user_time,
        )
        result[node_id] = union(waited)
    return result
