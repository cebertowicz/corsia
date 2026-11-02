"""Narrow Structured Plans reads required by onboarding.

Onboarding owns its consent and measurement behavior, while this module owns
the Plan-table shapes those behaviors need to inspect.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from infra.presence_time import parse
from plans import decision_projection, node_states, service


_HISTORICAL_RAISED_SQL = """
    SELECT e.created_at, i.metadata,
           p.id AS plan_id, n.id AS node_id, p.owner_id
      FROM plans AS p
      CROSS JOIN plan_nodes AS n
      CROSS JOIN plan_node_artifact_links AS l
      CROSS JOIN plan_node_events AS e
      CROSS JOIN artifact_identities AS i
     WHERE p.owner_kind = 'thread'
       AND p.owner_id = ?
       AND n.plan_id = p.id
       AND l.node_id = n.id
       AND l.role = 'attention'
       AND e.node_id = n.id
       AND e.event = 'decision_raised'
       AND i.id = l.artifact_id
       AND i.integration = 'local'
       AND i.type = 'plan-decision'
     ORDER BY e.created_at DESC, e.rowid DESC
"""


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def has_genuine_decision_raised_at_or_after(
    db, *, owner_thread_id: str, boundary: datetime,
) -> bool:
    """Whether this thread's own plan durably raised a genuine decision after boundary."""
    normalized_boundary = _naive_utc(boundary)
    for row in db.execute(_HISTORICAL_RAISED_SQL, (owner_thread_id,)):
        body = decision_projection._body(row)
        if body is None or (
            body["plan_id"], body["node_id"], body["thread_id"],
        ) != (row["plan_id"], row["node_id"], row["owner_id"]):
            continue
        try:
            raised_at = parse(row["created_at"])
        except (TypeError, ValueError):
            continue
        if raised_at >= normalized_boundary:
            return True
    return False


def decision_answer_actor(db, *, node_id: str) -> str | None:
    """Actor on the latest recorded answer for a decision source node."""
    row = db.execute(
        "SELECT actor_id FROM plan_node_events WHERE node_id=? "
        "AND event='decision_answered' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (node_id,),
    ).fetchone()
    return row["actor_id"] if row is not None else None


def decision_owner_id(db, *, node_id: str) -> str | None:
    """Thread owner of a decision node, if it belongs to a thread plan."""
    row = db.execute(
        "SELECT p.owner_id FROM plan_nodes n JOIN plans p ON p.id = n.plan_id "
        "WHERE n.id = ? AND p.owner_kind = 'thread'",
        (node_id,),
    ).fetchone()
    return str(row["owner_id"]) if row is not None else None


def decision_states(db, *, source_node_id: str, title: str) -> list[str]:
    """States of the well-formed decisions drafted from this node under this title."""
    row = db.execute("SELECT plan_id FROM plan_nodes WHERE id = ?", (source_node_id,)).fetchone()
    if row is None:
        return []
    return [
        item["state"] for item in decision_projection.rows_for_plans([row["plan_id"]], db=db)
        if item["source_node_id"] == source_node_id and item["title"] == title
    ]


def workflow_steps(db, *, thread_id: str, workflow_ref: str) -> list[dict]:
    """This thread's own plan's unarchived top-level steps of one workflow, in plan order."""
    rows = db.execute(
        "SELECT n.id, n.plan_id, n.state, n.workflow_step FROM plan_nodes AS n "
        "JOIN plans AS p ON p.id = n.plan_id WHERE p.owner_kind = 'thread' AND p.owner_id = ? "
        "AND n.workflow_ref = ? AND n.parent_id IS NULL AND " + node_states.unarchived_sql("n")
        + " ORDER BY n.order_key, n.rowid",
        (thread_id, workflow_ref),
    ).fetchall()
    return [
        {"id": row["id"], "plan_id": row["plan_id"], "state": row["state"],
         "workflow_step": row["workflow_step"], "settled": row["state"] in service._TERMINAL_STATES}
        for row in rows
    ]


def has_open_titled_node(db, *, thread_id: str, titles: Iterable[str]) -> bool:
    """Whether this thread's own plan holds an unsettled, unarchived node with one of these titles."""
    names = sorted(set(titles))
    if not names:
        return False
    rows = db.execute(
        "SELECT n.state FROM plan_nodes AS n JOIN plans AS p ON p.id = n.plan_id "
        "WHERE p.owner_kind = 'thread' AND p.owner_id = ? AND n.archived_at IS NULL "
        "AND n.title IN (%s)" % ",".join("?" * len(names)),
        (thread_id, *names),
    ).fetchall()
    return any(row["state"] not in service._TERMINAL_STATES for row in rows)
