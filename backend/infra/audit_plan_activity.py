"""The plan_activity audit metric: plan use per agent-day, conductor adds, stale tasks, owed verdicts.

Read through ``infra.audit_reads``, which binds the caller's scope and deadline.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from plans.public import automated_mutation_keys, owed_verdict_ages

#: The writes CORSIA makes for an agent, read from the keys their writers build.
_AUTOMATED = automated_mutation_keys.excluded()
_NOT_AUTOMATED = "".join(
    " AND NOT (r.operation=? AND r.mutation_id GLOB ?)" for _ in _AUTOMATED)


# Start with the user's indexed threads, then visit their plans, nodes, and
# node events through their owning indexes. Archived threads remain in scope.
_PLAN_SCOPE = (
    " FROM threads t INDEXED BY idx_threads_user "
    "CROSS JOIN plans p ON p.owner_kind='thread' AND p.owner_id=t.id "
    "CROSS JOIN plan_nodes n INDEXED BY idx_plan_nodes_tree ON n.plan_id=p.id "
)
_ARCHIVED = "CASE WHEN t.archived_at IS NOT NULL OR t.lifecycle_state='archived' THEN 1 ELSE 0 END"


_EVENT_WINDOW = (
    "e.created_at>=? AND e.created_at<? "
    "AND julianday(e.created_at)>=julianday(?) AND julianday(e.created_at)<julianday(?)"
)


def _activity_daily(db: sqlite3.Connection, user_id: str, bounds: tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    days = [dict(row) for row in db.execute(
        f"SELECT substr(a.started_at,1,10) AS day, {_ARCHIVED} AS archived, "
        "count(DISTINCT t.id) AS n FROM threads t INDEXED BY idx_threads_user "
        "CROSS JOIN active_turns a INDEXED BY idx_active_turns_session ON a.thread_id=t.id "
        "WHERE t.user_id=? AND a.started_at>=? AND a.started_at<? "
        "AND julianday(a.started_at)>=julianday(?) AND julianday(a.started_at)<julianday(?) "
        "GROUP BY day,archived ORDER BY day,archived", (user_id, *bounds)
    )]
    created = [dict(row) for row in db.execute(
        f"SELECT substr(e.created_at,1,10) AS day, {_ARCHIVED} AS archived, count(*) AS n "
        + _PLAN_SCOPE + "CROSS JOIN plan_node_events e INDEXED BY idx_plan_node_events_node "
        "ON e.node_id=n.id WHERE t.user_id=? AND e.event='node_created' AND " + _EVENT_WINDOW +
        " GROUP BY day,archived ORDER BY day,archived", (user_id, *bounds)
    )]
    return days, created


def plan_activity(db: sqlite3.Connection, user_id: str, since: str, until: str) -> list[dict]:
    """Read historic event counts and a separately timed current snapshot."""
    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()
    stale_before = (now - timedelta(days=7)).isoformat()
    # Bounds include complete UTC dates; julianday handles mixed ISO precision.
    start_day = datetime.fromisoformat(since).date().isoformat()
    end_day = (datetime.fromisoformat(until).date() + timedelta(days=1)).isoformat()
    bounds = (start_day, end_day, since, until)
    days, created = _activity_daily(db, user_id, bounds)
    from infra.corsia_agent import get_corsia_agent

    workspace_conductor = get_corsia_agent(db=db)
    adds = [dict(row) for row in db.execute(
        "SELECT CASE WHEN json_valid(e.payload_json) AND "
        "json_extract(e.payload_json,'$.conductor_kind') IN ('project','workspace') "
        "THEN json_extract(e.payload_json,'$.conductor_kind') "
        "WHEN e.actor_id=? THEN 'workspace' "
        "WHEN e.actor_id=(SELECT conductor_thread_id FROM projects WHERE id=t.project_id) "
        "THEN 'project' ELSE 'unknown' END AS conductor_kind_as_of_read, "
        "CASE WHEN json_valid(e.payload_json) AND "
        "json_extract(e.payload_json,'$.conductor_kind') IN ('project','workspace') "
        "THEN 'recorded' ELSE 'current_designation' END AS kind_basis, "
        "CASE WHEN n.state IN ('accepted','accepted_with_followup') THEN 'done' "
        "WHEN n.state='closed' THEN 'closed' WHEN n.state='failed' THEN 'failed' "
        "WHEN n.archived_at IS NOT NULL THEN 'archived' "
        "WHEN julianday(e.created_at)<julianday(?) THEN 'open_over_7_days' "
        "ELSE 'open_7_days_or_less' END AS outcome, "
        f"{_ARCHIVED} AS archived, count(*) AS n " + _PLAN_SCOPE +
        "CROSS JOIN plan_node_events e INDEXED BY idx_plan_node_events_node ON e.node_id=n.id "
        "WHERE t.user_id=? AND e.event='node_created' AND e.provenance='plan.conductor_add' AND " +
        _EVENT_WINDOW + " GROUP BY conductor_kind_as_of_read,kind_basis,outcome,archived "
        "ORDER BY conductor_kind_as_of_read,kind_basis,outcome,archived",
        (workspace_conductor, stale_before, user_id, *bounds),
    )]
    stale = [dict(row) for row in db.execute(
        f"SELECT {_ARCHIVED} AS archived, CASE WHEN n.id IN "
        "(SELECT plan_node_id FROM expected_event_watches WHERE status='armed' "
        "AND plan_node_id IS NOT NULL) THEN 1 ELSE 0 END AS armed_watch, count(*) AS n "
        + _PLAN_SCOPE +
        "WHERE t.user_id=? AND p.state='active' AND n.kind='task' "
        "AND n.archived_at IS NULL AND n.state IN ('pending','active','review_requested') "
        "AND NOT EXISTS (SELECT 1 FROM plan_node_events e INDEXED BY idx_plan_node_events_node "
        "WHERE e.node_id=n.id AND julianday(e.created_at)>julianday(?)) "
        "GROUP BY archived,armed_watch ORDER BY archived,armed_watch", (user_id, stale_before),
    )]
    calls = [dict(row) for row in db.execute(
        "SELECT substr(r.created_at,1,10) AS day, "
        "CASE WHEN a.archived_at IS NOT NULL OR a.lifecycle_state='archived' THEN 1 ELSE 0 END "
        "AS archived, count(*) AS n FROM threads t INDEXED BY idx_threads_user "
        "CROSS JOIN plans p ON p.owner_kind='thread' AND p.owner_id=t.id "
        "CROSS JOIN plan_mutation_receipts r ON r.plan_id=p.id "
        "CROSS JOIN threads a ON a.id=r.actor_id AND a.user_id=t.user_id "
        "WHERE t.user_id=? AND r.created_at>=? AND r.created_at<? "
        "AND julianday(r.created_at)>=julianday(?) AND julianday(r.created_at)<julianday(?) "
        "AND r.operation NOT GLOB 'decision_*' AND r.operation!='answer_worker_needs'"
        + _NOT_AUTOMATED + " GROUP BY day,archived ORDER BY day,archived",
        (user_id, *bounds, *(value for pair in _AUTOMATED for value in pair)),
    )]
    active_days = sum(row['n'] for row in days)
    update_calls = sum(row['n'] for row in calls)
    return [{
        "snapshot_as_of": snapshot_at,
        "active_agent_days": active_days,
        "active_agent_days_per_day": days,
        "nodes_created_per_day": created,
        "conductor_adds": adds,
        "stale_open_tasks": stale,
        "owed_verdicts": owed_verdict_ages(db, user_id=user_id, now=snapshot_at),
        "plan_update_calls": update_calls,
        "plan_update_calls_per_day": calls,
        "plan_update_calls_per_active_agent_day": update_calls / active_days if active_days else None,
        "plan_read_calls": None,
        "plan_read_calls_reason": "no_structured_source",
        "definitions": _PLAN_ACTIVITY_DEFINITIONS,
    }]


# Returned with the row, so only a caller of this metric reads them.
_PLAN_ACTIVITY_DEFINITIONS = {
    "scope": "This user's threads across projects, archived threads included; "
             "each count is split by the thread's current archive state.",
    "active_agent_days": "Distinct (thread, UTC day) pairs with a turn started, "
                         "whatever its outcome.",
    "plan_update_calls": "Accepted unique plan mutations by agent threads; a retried "
                         "mutation_id counts once and refusals are excluded. Excluded as "
                         "automated: attach_spawned_thread keys delegate_task-attach-* "
                         "and delegate_send-attach-*, start keys first-run-start:*, "
                         "archive_node keys first-run-app-reconcile:*, reorder keys "
                         "first-run-app-order:*, decision_* and answer_worker_needs. An "
                         "agent's own call that reuses one of those key prefixes is also "
                         "excluded, so this can undercount, never overcount.",
    "plan_read_calls": "Unavailable: no structured source records plan reads.",
    "conductor_adds": "Outcome is the node's current state; 'archived' is an unfinished "
                      "add that was later archived. kind_basis 'recorded' was stored at "
                      "creation; 'current_designation' uses today's conductor assignment.",
    "stale_open_tasks": "Snapshot at snapshot_as_of: unfinished tasks with no event in "
                        "the last 7 days. Event counts use the requested window.",
    "owed_verdicts": "Snapshot at snapshot_as_of: delegated steps a worker returned that "
                     "still await the parent's verdict, by age since the return; paused "
                     "plans and already-reminded steps included.",
}
