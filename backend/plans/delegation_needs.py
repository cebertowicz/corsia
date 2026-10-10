"""The one worker-side fact that says a parent owes an input."""

from __future__ import annotations

import uuid


def open_watches_sql(plan: str, columns: str = "need.id,need.plan_node_id") -> str:
    """Return the one canonical query for open worker-to-parent blocker watches.

    A need stays open past its deadline (``expired``) until the parent answers,
    but only while the worker's step is still open: settling the step ends it,
    whichever path settled it and whether or not that path resolved the watch.
    """
    return _open_watches_sql(f"={plan}", columns)


def _open_watches_sql(plan_scope: str, columns: str) -> str:
    """The open-need query over the plans ``plan_scope`` (``=x`` or ``IN (...)``) names."""
    from plans.node_states import open_sql

    return f"""SELECT {columns} FROM expected_event_watches need
        JOIN plan_node_events armed
          ON armed.node_id=need.plan_node_id AND armed.event='watch_armed'
         AND json_extract(armed.payload_json,'$.watch_id')=need.id
        JOIN plan_nodes need_step ON need_step.id=need.plan_node_id
        WHERE need.plan_id {plan_scope} AND need.status IN ('armed','expired')
          AND json_extract(armed.payload_json,'$.blocked.to_parent')=1
          AND {open_sql('need_step')}"""


def open_for_sql(plan: str) -> str:
    """Return the indexed predicate for an open ``to_parent`` blocker watch."""
    return f"EXISTS ({open_watches_sql(plan, '1')})"


#: Which open need is a plan's need when it has several: the oldest. The one
#: ordering both readers below use, so they can never name different reasons.
_OLDEST_NEED_FIRST = "need.armed_at,need.id"


def open_reason_sql(plan: str) -> str:
    """Return the oldest open parent-need reason from the canonical watch query."""
    columns = "json_extract(armed.payload_json,'$.blocked.reason')"
    return f"({open_watches_sql(plan, columns)} ORDER BY {_OLDEST_NEED_FIRST} LIMIT 1)"


def open_needs_by_plan_sql(plan_ids_sql: str) -> str:
    """Return ``(plan_id, reason)`` of each named plan's oldest open need, in one read.

    ``open_for_sql`` and ``open_reason_sql`` answer for one plan, so a reader
    that asks per delegating row walks every armed or expired watch once per
    row. This answers a set of plans at once: ``plan_ids_sql`` selects their
    ids, and a plan with no open need has no row. The predicate is
    ``open_watches_sql``'s and the ordering is ``_OLDEST_NEED_FIRST``.
    """
    columns = (
        "need.plan_id AS plan_id, json_extract(armed.payload_json,'$.blocked.reason') AS reason, "
        f"ROW_NUMBER() OVER (PARTITION BY need.plan_id ORDER BY {_OLDEST_NEED_FIRST}) AS need_rank"
    )
    return (
        "SELECT plan_id, reason FROM ("
        f"{_open_watches_sql(f'IN ({plan_ids_sql})', columns)}) WHERE need_rank=1"
    )


def open_for(plan, *, db) -> bool:
    """Whether this delegated worker plan still has an unanswered parent need."""
    return db.execute(
        f"SELECT {open_for_sql('?')} AS open_need", (plan["id"],),
    ).fetchone()["open_need"] == 1


def cause(row, owner_id: str) -> dict | None:
    """Project the parent-owned wait from one delegated-child fact row."""
    if not row["parent_need"]:
        return None
    return {
        "kind": "waiting", "reason_key": "worker_needs_input",
        "reason": "The delegated child needs your input: " + str(
            row["parent_need_reason"] or "input required",
        ),
        "owner_kind": "thread", "owner_id": owner_id,
        "child_thread_id": str(row["child_id"]), "created_at": row["attached_at"],
    }


def answer(parent: str, child: str, step: str, *, mutation_id: str | None = None, db=None) -> int:
    """Resolve every open parent-input watch after its answer has landed.

    This hook owns its plan write rather than joining the transport transaction:
    a process loss between delivery and this call leaves the worker's deadline
    intact, never falsely claiming that an unseen message answered it.
    """
    from automation import watches
    from plans import admission, receipts, service, watch_operations
    from plans.plan_write import transaction

    # The parent's send key lives in its own namespace inside the worker's plan.
    key = f"answer-worker-needs:{mutation_id or uuid.uuid4().hex}"
    with transaction(db) as connection:
        parent_plan = connection.execute(
            "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?", (parent,),
        ).fetchone()
        node = connection.execute(
            "SELECT plan_id,spawned_thread_id FROM plan_nodes WHERE id=?", (step,),
        ).fetchone()
        child_plan = connection.execute(
            "SELECT id,parent_plan_id FROM plans WHERE owner_kind='thread' AND owner_id=?", (child,),
        ).fetchone()
        if (
            parent_plan is None or node is None or child_plan is None
            or node["plan_id"] != parent_plan["id"] or node["spawned_thread_id"] != child
            or child_plan["parent_plan_id"] != parent_plan["id"]
        ):
            return 0
        params = {"child": child, "step": step}
        fingerprint = receipts.request_fingerprint("answer_worker_needs", params)
        replay = receipts.load(
            connection, plan_id=child_plan["id"], mutation_id=key,
            actor_id=parent, operation="answer_worker_needs", fingerprint=fingerprint,
        )
        if replay is not None:
            return int(replay["cleared"])
        rows = connection.execute(
            open_watches_sql("?", "need.id,need.plan_node_id") + " ORDER BY need.id",
            (child_plan["id"],),
        ).fetchall()
        for watch in rows:
            watches.resolve_watch(watch["id"], db=connection)
            watch_operations.record_resolved(
                connection, str(watch["plan_node_id"]), watch_id=watch["id"],
                expected_event=None, actor_id=parent, reason="answered_by_parent",
            )
        cleared = len(rows)
        receipts.record(
            connection, plan_id=child_plan["id"], mutation_id=key, actor_id=parent,
            operation="answer_worker_needs", fingerprint=fingerprint,
            result={"cleared": cleared},
        )
        if cleared:
            admission.reconcile_plan(
                str(child_plan["id"]), actor_id=parent,
                provenance="plan.answer_worker_needs", db=connection,
            )
        return cleared
