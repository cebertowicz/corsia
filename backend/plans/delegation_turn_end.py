"""What a parent hears when its worker's Turn ends: nothing returned, or a round over.

Split out of ``plans.delegation_notice`` (at its size cap), which still owns
the parent-notice table and the one wake writer (``_wake_parent``) this
module calls. Both rows here are decided only at the genuine end of a
worker's Turn (``automation.plan_liveness_terminal.on_thread_turn_complete``), because
no plan event says either of them:

- **A11, returned nothing.** The parent's step is still waiting on the worker,
  and the worker ended its Turn with no step left on its plan, so no return is
  coming. Keyed on the parent's step, not on the worker's plan alone: a worker
  that returned and then archived its settled steps has an empty plan too,
  but the parent already holds its work for a verdict (#2009).
- **A step failed with work still open.** A step the worker failed during this
  Turn, while other work stays open, is the parent's to decide about now;
  the worker's *last* step failing is its return and the harvest carries it
  (#2216). Decided here rather than at the failure, because the worker may
  still retry or re-scope it before its Turn ends.
- **W5, held only by the user.** The worker's open work is held by blockers
  only the user clears and nothing else can move or wake it: no work it can
  move, no armed wait. No one looks at a subagent's plan, so the parent hears
  once and can take it to the user. A plan stopped only by the user's own
  pause is the user's choice: the parent already has its digest, and nothing
  wakes a user's pause.
- **D4, a round after the return.** The parent sent a returned worker back by
  message instead of rejecting. While that round runs the parent's step reads
  as waiting on the worker (``wait_causes``), so liveness is quiet; the
  round's end is the return the parent is waiting for, and the harvest will
  not report it (it reports each attachment once).
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from plans import node_states

#: The parent's step is still waiting on this worker to return anything.
WAITING_STATES = ("pending", "active")


def parent_waiting(connection, plan_id: str) -> tuple[str, str] | None:
    """Return (parent plan, worker thread) while the parent waits on this worker.

    It waits while the worker holds open delegated work, or while its
    delegated step is still pending or active: a worker whose first Turn
    errored has an empty plan, but its parent still waits on it (#501).
    """
    from plans import projections
    from plans.delegation_notice import _delegation

    delegation = _delegation(connection, plan_id)
    if delegation is None or projections.open_delegated_work(connection, plan_id):
        return delegation
    step = projections.delegated_step(connection, *delegation)
    return delegation if step is not None and step["state"] in WAITING_STATES else None


def on_worker_turn_end(
    connection, thread_id: str, *, changed_plan_ids: list[str] | None = None,
    turn_errored: bool = False,
) -> bool:
    """Wake the parent once if its worker's Turn ended with no return coming.

    True when a row here told the parent about this Turn end, so the generic
    stopped-with-work-open wake (F2) does not tell it twice. Then the
    parent's step takes its slot back if the worker's wait on it just cleared;
    after the news, so the selection is not a second message about it.
    """
    from plans import delegated_selection

    plan = connection.execute(
        "SELECT id,parent_plan_id FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchone()
    if plan is None or plan["parent_plan_id"] is None:
        return False
    told = _tell_parent(
        connection, plan, changed_plan_ids=changed_plan_ids, turn_errored=turn_errored,
    )
    delegated_selection.reconcile_delegator(str(plan["id"]), db=connection)
    return told


def _tell_parent(
    connection, plan, *, changed_plan_ids: list[str] | None, turn_errored: bool,
) -> bool:
    """Write the one Turn-end row that applies to the parent's step, if any."""
    from plans import projections
    from plans.delegation_notice import _delegation

    delegation = _delegation(connection, str(plan["id"]))
    if delegation is None:
        return False
    parent_plan_id, worker = delegation
    step = projections.delegated_step(connection, parent_plan_id, worker)
    if step is None:
        return False
    if step["state"] in WAITING_STATES:
        nothing = (
            False if turn_errored
            else _returned_nothing(
                connection, str(plan["id"]), step, parent_plan_id, worker,
                changed_plan_ids=changed_plan_ids,
            )
        )
        failed = _step_failed(connection, str(plan["id"]), parent_plan_id, worker)
        if nothing or failed:
            return True
        return _held_by_user(connection, str(plan["id"]), parent_plan_id, worker)
    if step["state"] == "review_requested":
        return _round_returned(
            connection, str(plan["id"]), step, parent_plan_id, worker,
            changed_plan_ids=changed_plan_ids,
        )
    return False


def _returned_nothing(
    connection, plan_id: str, step, parent_plan_id: str, worker: str,
    *, changed_plan_ids: list[str] | None = None,
) -> bool:
    """A11: the parent waits on a worker whose plan holds no step at all.

    A worker with no plan that ended its Turn with a reply has returned that
    reply (#282): the step moves to review through the harvest, which is the
    parent's one "has returned" wake. Only a Turn that said nothing is A11.

    It applies only to a Turn that ended without a provider error. Once per
    empty plan revision, so a worker that ends several Turns still
    empty does not nag. A plan whose every step was closed is not empty: the
    harvest carries those closed steps to the parent as the return.
    """
    from plans import projections
    from plans.delegation_notice import _wake_parent

    live = connection.execute(
        "SELECT 1 FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL LIMIT 1",
        (plan_id,),
    ).fetchone()
    if live is not None or projections.delegated_work_returned(connection, plan_id):
        return False
    from plans import delegation_reply

    reply = turn_reply(connection, worker)
    if reply is not None and delegation_reply.harvest_reply(
        connection, step=step, reply=reply["text"], changed_plan_ids=changed_plan_ids,
    ):
        return True
    _wake_parent(
        connection, parent_plan_id, worker,
        kind="child_returned_nothing", subject_id=plan_id, child_plan_id=plan_id,
    )
    return True


def _step_failed(connection, plan_id: str, parent_plan_id: str, worker: str) -> bool:
    """A step failed during this Turn and unreturned work remains. Once per Turn."""
    from plans import node_states, projections
    from plans.delegation_notice import _wake_parent

    turn = latest_turn(connection, worker)
    if turn is None or projections.delegated_work_returned(connection, plan_id):
        return False
    failed = [
        # A failure under a closed step was closed with it: not news.
        str(row["id"]) for row in connection.execute(
            f"""SELECT DISTINCT n.id,e.created_at FROM plan_node_events e
               JOIN plan_nodes n ON n.id=e.node_id
               WHERE n.plan_id=? AND n.state='failed' AND n.archived_at IS NULL
                 AND {node_states.outside_retired_branch_sql('n')}
                 AND e.event='state_transition' AND e.new_state='failed'""",
            (plan_id,),
        ).fetchall()
        if instant(row["created_at"]) >= instant(turn["started_at"])
    ]
    if not failed:
        return False
    _wake_parent(
        connection, parent_plan_id, worker,
        kind="child_step_failed", subject_id=str(turn["id"]), revision=0,
        extra={"failed_node_ids": sorted(set(failed))},
    )
    return True


#: Open blockers only the user clears on the worker's open tasks still in work.
_USER_BLOCKERS_SQL = f"""
    SELECT h.id,h.reason,n.title FROM plan_node_holds h
      JOIN plan_nodes n ON n.id=h.node_id
     WHERE n.plan_id=? AND {node_states.in_work_sql('n')}
       AND {node_states.outside_retired_branch_sql('n')}
       AND n.state IN ('pending','active','review_requested')
       AND h.cleared_at IS NULL AND h.kind='blocked' AND h.owner_kind='user'
     ORDER BY h.created_at,h.id
"""


def _held_by_user(connection, plan_id: str, parent_plan_id: str, worker: str) -> bool:
    """W5: the worker's open work waits only on the user. Once per set of blockers.

    Keyed on the blockers themselves, so Turns ending with the same ones held
    do not nag, and a new blocker is new news.
    """
    from plans import projections
    from plans.delegation_notice import _wake_parent

    active = connection.execute(
        "SELECT 1 FROM plans WHERE id=? AND state='active'", (plan_id,),
    ).fetchone()
    if active is None or projections.movable_work(connection, plan_id):
        return False
    armed = connection.execute(
        "SELECT 1 FROM expected_event_watches WHERE plan_id=? AND status='armed' LIMIT 1",
        (plan_id,),
    ).fetchone()
    blockers = connection.execute(_USER_BLOCKERS_SQL, (plan_id,)).fetchall()
    if armed is not None or not blockers:
        return False
    held = "\n".join(sorted(str(row["id"]) for row in blockers))
    _wake_parent(
        connection, parent_plan_id, worker,
        kind="child_stopped_on_user",
        subject_id=f"user-held-{hashlib.sha256(held.encode()).hexdigest()[:16]}",
        revision=1, task=str(blockers[0]["title"]),
        reason=str(blockers[0]["reason"] or ""),
    )
    return True


def latest_turn(connection, worker: str):
    """The worker's most recent Turn, the one whose end is being decided."""
    return connection.execute(
        """SELECT id,started_at FROM active_turns WHERE thread_id=?
           ORDER BY turn_order DESC LIMIT 1""",
        (worker,),
    ).fetchone()


def _round_returned(
    connection, plan_id: str, step, parent_plan_id: str, worker: str,
    *, changed_plan_ids: list[str] | None = None,
) -> bool:
    """D4: a round the worker began after its return has ended with nothing open.

    A Turn that began before the harvest is the one the harvest already
    reported. Only this worker's own harvest counts: a replacement never
    returned what its predecessor did. A round that left work the worker can
    still move is not over: its own liveness carries it until it returns. Nor
    is one that waits on the user (G10): the worker acts on the answer first.
    Once per Turn. ``harvest`` below is the latest return of this worker: the
    first harvest or a later round already recorded here.

    A step the owner has already returned to its own parent is no longer the
    worker's verdict to settle: the parent is told, but no boundary or revision
    moves under the grandparent's pending review.
    """
    from plans import delegation_harvest, projections
    from plans.delegation_notice import _wake_parent

    harvest = connection.execute(
        """SELECT created_at FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'
             AND json_extract(payload_json,'$.child_plan_id')=?
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (step["id"], plan_id),
    ).fetchone()
    turn = latest_turn(connection, worker)
    if harvest is None or turn is None:
        return False
    if instant(turn["started_at"]) <= instant(harvest["created_at"]):
        return False
    if projections.movable_work(connection, plan_id) or delegation_harvest.waiting_on_answer(
        connection, plan_id,
    ):
        return False
    if not delegation_harvest.awaiting_owner_verdict(connection, step):
        _wake_parent(
            connection, parent_plan_id, worker,
            kind="child_round_returned", subject_id=str(turn["id"]), revision=0,
        )
        return True
    return delegation_harvest.record_round_return(
        connection,
        node=step,
        child_plan_id=plan_id,
        parent_plan_id=parent_plan_id,
        worker_thread_id=worker,
        turn_id=str(turn["id"]),
        changed_plan_ids=changed_plan_ids,
    )


#: How much of the worker's final reply travels as its return or in a wake.
FINAL_REPLY_CHARS = 2000


def turn_reply(connection, worker: str):
    """What the worker said as its last words this Turn: ``{id, text}``, or None.

    The latest non-empty assistant message, only if the worker's latest Turn
    began before it: an older reply may be a question already answered. Not
    every harness passes its Turn id here, so the bound is that Turn's start.
    The question or result usually ends the reply, so the tail is what travels.
    """
    from infra.thread_confinement import (
        ISOLATED_RESULT_LABEL, finished_agent_result, restrict_agent_read,
    )

    thread = connection.execute("SELECT confinement FROM threads WHERE id=?", (worker,)).fetchone()
    if thread and restrict_agent_read(thread[0], worker, ""):
        final = finished_agent_result(connection, worker)
        return None if final is None else {
            "id": final["id"], "text": f"{ISOLATED_RESULT_LABEL}\n\n{final['content']}",
        }
    last = connection.execute(
        """SELECT id,content,created_at FROM messages WHERE thread_id=? AND seq IS NOT NULL
             AND role='assistant' AND length(trim(content)) > 0
           ORDER BY seq DESC LIMIT 1""",
        (worker,),
    ).fetchone()
    turn = latest_turn(connection, worker)
    if last is None or (
        turn is not None and instant(last["created_at"]) < instant(turn["started_at"])
    ):
        return None
    text = str(last["content"]).strip()
    if len(text) > FINAL_REPLY_CHARS:
        text = "…" + text[-FINAL_REPLY_CHARS:]
    return {"id": str(last["id"]), "text": text}


def instant(value: str) -> datetime:
    """Read a stored timestamp as UTC; Turn rows store naive UTC, plans aware."""
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
