"""Attempt-row persistence subordinate to the canonical admission owner."""

from __future__ import annotations

import logging
import sqlite3
import uuid

import plans.service as service

logger = logging.getLogger(__name__)


def open_attempt(db, node_id: str):
    """Return the node's one open attempt row, or None."""
    return open_attempts([node_id], db=db).get(str(node_id))


def open_attempts(node_ids, *, db, compact: bool = False) -> dict:
    """Unreleased attempts; compact callers select only the validity identity."""
    from plans import node_waits

    ids = list(node_ids)
    if not ids:
        return {}
    columns = "id,node_id,executor_thread_id,requirements_revision" if compact else "*"
    return {
        str(row["node_id"]): row
        for row in db.execute(
            f"""SELECT {columns} FROM plan_node_attempts
                WHERE node_id IN (SELECT value FROM json_each(?)) AND released_at IS NULL""",
            (node_waits.encoded_ids(ids),),
        )
    }


def executor_start(db, node_id: str) -> tuple[str, str] | None:
    """Return the task's executor and when it first selected the task, or None.

    Only the thread that runs the task counts. An attempt by a thread the task
    has since left is superseded (``admission_projection.superseded_by``) and
    is not that thread's work on it. None when the executor never selected it.
    The minimum is taken through ``julianday`` rather than as text: older
    attempts carry a shorter timestamp format than current ones.

    None, with a warning, when any of the executor's attempts has a start that
    cannot be read as a time: the first start is then unknown, and a later
    attempt must never stand in for it. ``latest_accepted_starts`` applies the
    same rule. Malformed legacy or foreign rows are rejected safely by this
    guard: they are skipped and logged rather than treated as a known start.
    """
    from plans.admission_projection import executor_of

    node = service._node_row(db, node_id)
    executor = executor_of(node, service._plan_row(db, node["plan_id"]))
    if executor is None:
        return None
    first = db.execute(
        """SELECT selected_at, MIN(julianday(selected_at)),
                  COUNT(*) - COUNT(julianday(selected_at)) AS unreadable
             FROM plan_node_attempts
            WHERE node_id=? AND executor_thread_id=?""",
        (node_id, executor),
    ).fetchone()
    if first["unreadable"]:
        logger.warning("task %s has an attempt with an unreadable start; start unknown", node_id)
        return None
    if first["selected_at"] is None:
        return None
    return executor, str(first["selected_at"])


def latest_accepted_starts(db) -> dict[str, str]:
    """Return, per thread, the latest first start among the tasks it ran to acceptance.

    The same executor rule as ``executor_start``, over every accepted task.
    A task with any attempt whose start cannot be read as a time has no known
    first start, so it is left out whole and logged: one bad row must not fail
    the pass for every thread, and a later attempt must never stand in for a
    first start that could not be read. Malformed legacy or foreign rows are
    safely skipped and logged rather than treated as known starts.
    """
    from plans.admission_projection import executor_of

    accepted = sorted(service._ACCEPTED_STATES)
    marks = ",".join("?" for _ in accepted)
    rows = db.execute(
        f"""SELECT a.executor_thread_id AS attempt_executor, a.node_id,
                   a.selected_at, julianday(a.selected_at) AS selected_day,
                   n.executor_thread_id, n.owner_thread_id, p.owner_kind, p.owner_id
              FROM plan_node_attempts AS a
              JOIN plan_nodes AS n ON n.id=a.node_id
              JOIN plans AS p ON p.id=n.plan_id
             WHERE n.state IN ({marks})""",
        accepted,
    ).fetchall()
    first: dict[tuple[str, str], tuple[float, str]] = {}
    unknown: set[tuple[str, str]] = set()
    for row in rows:
        executor = str(row["attempt_executor"])
        if executor != executor_of(row, row):
            continue
        task = (executor, str(row["node_id"]))
        if row["selected_day"] is None:
            logger.warning(
                "attempt on task %s has an unreadable start %r; task not counted",
                row["node_id"], row["selected_at"],
            )
            unknown.add(task)
            continue
        start = (float(row["selected_day"]), str(row["selected_at"]))
        first[task] = min(first.get(task, start), start)
    latest: dict[str, tuple[float, str]] = {}
    for task, start in first.items():
        if task in unknown:
            continue
        executor = task[0]
        latest[executor] = max(latest.get(executor, start), start)
    return {executor: start[1] for executor, start in sorted(latest.items())}


def effects_in_flight(db, attempt_ids) -> dict[str, int]:
    """Count each attempt's unsettled dispatches.

    The one answer to "is an effect in flight on this attempt", for the
    projection's in-flight count. It never keeps a slot: `release` does not
    wait on it (#2705). Every dispatch row is a started call whose effect may still land;
    Plans' own tools record none, because whatever they change commits inside
    the Plans write itself. Attempts with nothing in flight are absent from the
    result.
    """
    ids = [str(attempt_id) for attempt_id in attempt_ids]
    if not ids:
        return {}
    id_marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT attempt_id,COUNT(*) AS count FROM plan_tool_dispatches
            WHERE attempt_id IN ({id_marks}) AND settled_at IS NULL
            GROUP BY attempt_id""",
        ids,
    ).fetchall()
    return {str(row["attempt_id"]): int(row["count"]) for row in rows}


def current_for_executor(db, executor: str):
    """Return the open attempts in the executor's own thread plan, for attribution.

    Only a plan's owner runs its tasks, so a thread's calls are served only by
    its own plan, where one open attempt per plan and executor leaves at most
    one row. ``selection_validity.valid_selections`` decides whether it is
    still valid.
    """
    return db.execute(
        """SELECT a.*,n.commitment_id
           FROM plan_node_attempts a
           JOIN plan_nodes n ON n.id=a.node_id
           JOIN plans p ON p.id=a.plan_id
           WHERE a.executor_thread_id=? AND a.released_at IS NULL
             AND p.owner_kind='thread' AND p.owner_id=?
           ORDER BY a.selected_at,a.id""",
        (executor, executor),
    ).fetchall()


def turn_binding(db, executor: str):
    """Return the executor's live Turn row, or None."""
    return db.execute(
        """SELECT * FROM active_turns
           WHERE thread_id=? AND status='running'
           ORDER BY turn_order DESC LIMIT 1""",
        (executor,),
    ).fetchone()


#: What it means for an attempt to occupy an executor's slot in a plan.
#:
#: This has to mean exactly what the database means. The unique index
#: `idx_plan_attempt_open_executor` is on (plan_id, executor_thread_id) WHERE
#: released_at IS NULL and cannot filter node state, because a SQLite partial
#: index cannot reference another table. So an open attempt row occupies the
#: slot, full stop — including one that outlived its work because `release`
#: deferred while a tool effect was still in flight.
#:
#: Filtering node state here instead produced a subtler version of the bug it
#: was meant to fix: both projections agreed the slot was free while the index
#: still refused the insert, so the agent was told `start` and got "task is
#: not ready to start". Occupancy is a fact the database owns; the projected
#: conflict names the occupying node and its state so a reader can see whether
#: it is still working or waiting for an in-flight call to settle.
OCCUPIES_SLOT_SQL = "a.released_at IS NULL"


def slot_holders(db, plan_ids, executors) -> dict[tuple[str, str], sqlite3.Row]:
    """Return the attempt occupying each (plan, executor) slot, in one query.

    The only occupancy read: selection's conflict check, the projected
    conflict and a closing result's ``next`` block all derive from it, so what
    blocks selection and what an agent is told blocks it cannot drift apart.
    Each row carries the attempt's columns plus its task's ``title``, ``state``
    and ``revision``. The partial unique index allows at most one per slot.
    """
    plans = sorted({str(plan_id) for plan_id in plan_ids})
    threads = sorted({str(executor) for executor in executors})
    if not plans or not threads:
        return {}
    plan_marks = ",".join("?" for _ in plans)
    thread_marks = ",".join("?" for _ in threads)
    rows = db.execute(
        f"""SELECT a.*,n.title,n.state,n.revision FROM plan_node_attempts a
            JOIN plan_nodes n ON n.id=a.node_id
            WHERE a.plan_id IN ({plan_marks})
              AND a.executor_thread_id IN ({thread_marks})
              AND {OCCUPIES_SLOT_SQL}""",
        (*plans, *threads),
    ).fetchall()
    return {(str(row["plan_id"]), str(row["executor_thread_id"])): row for row in rows}


def slot_holder(db, plan_id: str, executor: str):
    """Return the attempt occupying one executor's slot in a plan, or None."""
    return slot_holders(db, [plan_id], [executor]).get((str(plan_id), str(executor)))


def selection_conflict(db, node, executor: str) -> dict | None:
    """Return the task occupying this executor's slot in the node's plan."""
    holder = slot_holder(db, node["plan_id"], executor)
    if holder is None or holder["node_id"] == node["id"]:
        return None
    return {
        "condition": "executor_selected",
        "selected_node_id": holder["node_id"],
        "selected_node_title": holder["title"],
        "selected_node_state": holder["state"],
    }


def insert(db, node, executor: str, *, actor_id: str | None, provenance: str):
    """Insert the node's next attempt for one executor and return its row.

    Raises:
        sqlite3.IntegrityError: The node or the executor's plan slot already
            holds an open attempt.
    """
    ordinal = int(
        db.execute(
            """SELECT COALESCE(MAX(attempt_ordinal),0)+1
               FROM plan_node_attempts WHERE node_id=?""",
            (node["id"],),
        ).fetchone()[0]
    )
    attempt_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO plan_node_attempts
           (id,plan_id,node_id,executor_thread_id,requirements_revision,
            attempt_ordinal,selected_by_actor_id,provenance,selected_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            attempt_id, node["plan_id"], node["id"], executor,
            node["requirements_revision"], ordinal, actor_id,
            service._required_text(provenance, "provenance"), service._now(),
        ),
    )
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def bind_turn(db, node, attempt, executor: str) -> None:
    """Record the attempt as the executor's live Turn accounting, if still unbound.

    The binding owner (`infra.turn_plan_binding`) also moves the usage the Turn
    recorded before this mid-Turn selection onto the attempt (#579).
    """
    turn = turn_binding(db, executor)
    if turn is None:
        return
    from infra.turn_plan_binding import bind_turn_to_plan_attempt
    from plans import commitments

    bind_turn_to_plan_attempt(
        db,
        turn_id=turn["id"],
        plan_id=node["plan_id"],
        node_id=node["id"],
        attempt_id=attempt["id"],
        commitment_id=node["commitment_id"],
        usage_role=commitments.usage_role(node["id"], db=db),
    )




def release(node_id: str, reason: str, *, db) -> bool:
    """Close the node's open selection now, freeing its executor's slot.

    Never waits on a started call: parking a task must free the slot at once.
    A call still open keeps its dispatch row and attempt id, so its effect is
    still attributed to this attempt when it settles. Waiting on the row held
    the slot for the whole Turn whenever a settlement was lost (#2705).
    """
    attempt = open_attempt(db, node_id)
    if attempt is None:
        return False
    changed = db.execute(
        """UPDATE plan_node_attempts SET released_at=?,release_reason=?
           WHERE id=? AND released_at IS NULL""",
        (service._now(), service._required_text(reason, "release reason"), attempt["id"]),
    )
    return changed.rowcount == 1
