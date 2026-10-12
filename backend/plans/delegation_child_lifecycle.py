"""Wake the parent when a delegated child stops without saying so itself.

Rows F1-F3 and F8 of the child-parent notification matrix: section A's wakes
are triggered by something the child *did* to its own plan; these catch a
child that *stopped* -- its Turn errored, it ended a Turn with work open and
nothing recorded (often a question for the parent), its thread is simply
gone, or its running Turn has gone silent -- while its parent still holds
open work waiting on it. Reuses ``delegation_notice._delegation``
and ``._wake_parent`` so there is still one writer of what a parent hears.
"""

from __future__ import annotations

import os

#: The provenance of every write made because a child thread is gone.
_ENDED = "lifecycle_child_ended"


def notify_turn_error(
    connection,
    thread_id: str,
    *,
    turn_id: str | None = None,
    error: str,
    resumes_at: str | None = None,
) -> None:
    """Notify open delegated work, including pre-Turn account heads, once per episode.
    Plan, fixed cause and parent's last message identify the existing episode. ``resumes_at`` is the reset wait the worker's liveness actually armed, never a time merely quoted.
    """
    from datetime import datetime, timezone
    import hashlib

    from plans import delegation_turn_end, wait_causes
    from plans.delegation_child_rollover import has_rollover_successor
    from plans.delegation_notice import _wake_parent
    from prompts import plan_wakes as text
    from subagents.provider_usage_limits import is_provider_usage_or_quota_limit

    plan = connection.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return
    delegation = delegation_turn_end.parent_waiting(connection, str(plan["id"]))
    if delegation is None:
        return
    parent_plan_id, worker = delegation

    def episode(cause: str) -> str:
        sent = connection.execute(  # newest first: stops at the parent's latest input
            """SELECT m.input_order AS last FROM messages AS m JOIN plans AS p ON p.id=?
               WHERE m.thread_id=? AND m.input_order IS NOT NULL AND json_valid(m.metadata)
                 AND json_extract(m.metadata,'$.from_thread')=p.owner_id
               ORDER BY m.input_order DESC LIMIT 1""",
            (parent_plan_id, worker),
        ).fetchone()
        key = f"{cause}\n{sent['last'] if sent is not None else None}"
        return f"{worker}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"

    # A head held for its model has the same recovery as a limit (#3317).
    held_kind = wait_causes.stalled_why(connection, thread_id)
    held = held_kind == "model"
    if held_kind != 'account' and not held and not is_provider_usage_or_quota_limit(error):
        _wake_parent(
            connection, parent_plan_id, worker, kind="child_turn_errored",
            subject_id=episode(error), child_plan_id=str(plan["id"]), error=error,
        )
        return
    model = connection.execute(
        """SELECT COALESCE(
             (SELECT model FROM active_turns WHERE id=? AND thread_id=?),
             (SELECT model FROM threads WHERE id=?)) AS model""",
        (turn_id, worker, worker),
    ).fetchone()
    refused_on = (model["model"] if model is not None else None) or "its current model"
    if held_kind == 'account':
        from connections import read_account_readiness

        state, fields = text.account_wake_fields(read_account_readiness([worker], db=connection).get(worker), error)
        _wake_parent(connection, parent_plan_id, worker, kind='child_provider_refused',
                     subject_id=episode(f'account\n{refused_on}\n{error}'), child_plan_id=str(plan['id']),
                     model=refused_on, account_hold_state=state, **fields)
        return
    if not held and has_rollover_successor(connection, worker, turn_id):  # recovered on another account
        return
    resume_note = (
        text.REFUSED_RESUMES_AT.format(
            at=datetime.fromisoformat(resumes_at).astimezone(timezone.utc),
        )
        if resumes_at else text.REFUSED_HELD if held else text.REFUSED_NO_RESET
    )
    _wake_parent(
        connection, parent_plan_id, worker, kind="child_provider_refused",
        subject_id=episode(f"{refused_on}\n{resumes_at}"),
        child_plan_id=str(plan["id"]), error=error,
        model=refused_on, resume_note=resume_note,
    )


def _waits_elsewhere(connection, plan) -> bool:
    """Whether the worker recorded what it waits on: a watch or a block on open work.

    Such a worker waits on that event or owner, not on its parent, and the
    parent already hears the wait through the child-watch and child-blocker
    digests. Other ready work left in its plan does not change that (#2637).
    """
    from plans import projections

    _actionable, projected = projections.actionable_leaves(connection, plan)
    return any(
        cause["kind"] == "waiting" or (cause.get("placed") and cause["kind"] == "blocked")
        for projection in projected.values()
        for cause in projection["causes"]
    )


def notify_stopped_open(connection, thread_id: str, *, wake_id: str) -> None:
    """F2: the child ended a Turn with movable work open and no one working it.

    Often it is asking the parent something, so the wake carries the reply
    this Turn made. A new reply is new news, even with the plan unchanged
    (the parent's delegate_send answer moves no revision, debt 84), so the
    wake is keyed on that message. A Turn that said nothing is keyed on the
    liveness row ``wake_id`` and the child plan revision, so silent Turns
    ending unchanged are told once. Only while the parent's step still waits
    on the worker: a step up for review is D4's, and a worker that recorded
    what it waits on is not waiting on its parent (``_waits_elsewhere``).
    """
    from plans import projections
    from plans.delegation_notice import _delegation, _wake_parent
    from plans.delegation_turn_end import WAITING_STATES, turn_reply

    plan = connection.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchone()
    if plan is None or _waits_elsewhere(connection, plan):
        return
    delegation = _delegation(connection, str(plan["id"]))
    if delegation is None:
        return
    parent_plan_id, worker = delegation
    step = projections.delegated_step(connection, parent_plan_id, worker)
    if step is None or step["state"] not in WAITING_STATES:
        return
    last = turn_reply(connection, worker)
    final = last["text"] if last is not None else ""
    _wake_parent(
        connection, parent_plan_id, worker,
        kind="child_stopped_open",
        subject_id=f"message-{last['id']}" if last is not None else wake_id,
        child_plan_id=str(plan["id"]),
        extra={"final_message": final or None},
    )


#: How long a delegated worker's running Turn may go without a model call or a
#: message before its parent is told. No normally finishing Turn in a week of
#: production went this long silent (plans-silent-child-2026-09-25.md).
SILENT_AFTER_SECONDS = int(os.getenv("CORSIA_SILENT_CHILD_SECONDS", "1200"))


_SILENT = "child_silent"


def silent_children(connection, *, now: str, turn_ids: list[str] | None = None) -> list:
    """F8's candidates: running delegated Turns silent past the threshold, not yet told.

    A plain read, safe outside any write: the sweep asks this first and takes
    the writer only when it names someone (#2748). ``turn_ids`` narrows the
    scan to Turns already found, for the recheck inside the write. A silence
    episode whose wake already exists is not a candidate, so a worker silent
    for hours costs no write on each later sweep.
    """
    from plans import node_states, plan_wake
    from plans.delegation import _OPEN_PARENT_STATES

    # F8 measures a running worker's liveness while its attachment remains
    # open, including review_requested. F2 instead decides terminal return
    # ownership, where review_requested belongs to D4's returned round.
    placeholders = ",".join("?" for _ in _OPEN_PARENT_STATES)
    only = "" if turn_ids is None else (
        f" AND t.id IN ({','.join('?' for _ in turn_ids)})"
    )
    rows = connection.execute(
        f"""WITH running AS (
                 SELECT t.id AS turn_id, t.thread_id AS worker,
                        MAX(julianday(t.started_at),
                            COALESCE((SELECT julianday(MAX(u.created_at))
                                      FROM llm_usage AS u
                                      WHERE u.thread_id=t.thread_id), 0),
                            COALESCE((SELECT julianday(MAX(m.created_at))
                                      FROM messages AS m
                                      WHERE m.thread_id=t.thread_id
                                        AND m.seq IS NOT NULL
                                        AND m.role IN ('assistant', 'tool')
                                        AND length(trim(m.content)) > 0), 0)) AS last
                 FROM active_turns AS t WHERE t.status='running'{only})
            SELECT DISTINCT r.turn_id, r.worker, n.plan_id,
                   strftime('%Y-%m-%dT%H:%M:%SZ', r.last) AS since,
                   strftime('%Y-%m-%d %H:%M', r.last) AS shown,
                   CAST((julianday(?) - r.last) * 1440 AS INTEGER) AS minutes
            FROM running AS r
            JOIN plan_nodes AS n
              ON n.spawned_thread_id=r.worker AND {node_states.in_work_sql('n')}
             AND n.state IN ({placeholders})
            WHERE julianday(?) - r.last >= ? / 86400.0
            ORDER BY r.turn_id, n.plan_id""",
        (*(turn_ids or ()), now, *_OPEN_PARENT_STATES, now, SILENT_AFTER_SECONDS),
    ).fetchall()
    return [
        row for row in rows
        if connection.execute(
            "SELECT 1 FROM messages WHERE id=?",
            (plan_wake.wake_id(_SILENT, f"{row['turn_id']}:{row['since']}", 0),),
        ).fetchone() is None
    ]


def notify_silent_children(connection, *, now: str, turn_ids: list[str]) -> None:
    """F8: a worker's Turn is still running but has said and called nothing.

    Runs inside the plan write and rechecks ``turn_ids``, the candidates
    ``silent_children`` found before the write began. Last progress is the
    latest of the Turn's start, the worker's last model call and its last
    message. Each silence episode wakes the parent once: the episode is the
    Turn plus that last-progress instant, so progress followed by a new
    silence is a new wake. Nothing is stopped or restarted; what to do about a
    stuck worker is the parent's call.
    """
    from plans.delegation_notice import _wake_parent

    for row in silent_children(connection, now=now, turn_ids=turn_ids):
        _wake_parent(
            connection, str(row["plan_id"]), str(row["worker"]),
            kind=_SILENT, subject_id=f"{row['turn_id']}:{row['since']}",
            revision=0, minutes=int(row["minutes"]), since=str(row["shown"]),
        )


def end_open_work(
    connection, thread_id: str, *, reason: str, provenance: str,
    actor_id: str | None = None,
) -> list[str]:
    """Close every open step a worker holds and return the workers it had delegated to.

    Shared by a gone worker (F3) and a replaced one (F6). A step that still
    names a worker of its own is detached first: the archive guard refuses to
    retire a step with unsettled delegated work, and nobody is left to settle
    it. Those workers keep running, so they are returned for the caller to name.
    Closing a parent's last open child makes that parent an open leaf, so the
    pass repeats until the plan holds none, a started step whose own steps
    settled included (``projections.open_delegated_node_ids``).
    """
    from plans import closure, delegation, projections

    plan = connection.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return []
    stranded: list[str] = []
    while open_ids := projections.open_delegated_node_ids(connection, str(plan["id"])):
        for node_id in open_ids:
            own = connection.execute(
                "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
            ).fetchone()["spawned_thread_id"]
            if own:
                stranded.append(str(own))
                delegation.detach_spawned_thread(
                    node_id, reason=reason, actor_id=actor_id,
                    provenance=provenance, db=connection,
                )
            closure.close_node(
                node_id, reason=reason, actor_id=actor_id,
                provenance=provenance, db=connection,
            )
    return stranded


def _live_parent_nodes(connection, thread_id: str):
    from plans.delegation import _OPEN_PARENT_STATES

    placeholders = ",".join("?" for _ in _OPEN_PARENT_STATES)
    return connection.execute(
        f"""SELECT id,plan_id FROM plan_nodes
            WHERE spawned_thread_id=? AND archived_at IS NULL
              AND state IN ({placeholders})""",
        (thread_id, *_OPEN_PARENT_STATES),
    ).fetchall()


def on_child_thread_archived(connection, thread_id: str, *, archived_at: str) -> None:
    """F3, reversible half: a user archived the child; tell its parent only.

    A user archive can be undone, so nothing irreversible happens here: the
    child's leaves stay open and the attachment stays. The parent can still
    close or re-delegate its step, because an archived child is unreachable
    (``archival._child_thread_reachable``), and a later purge or delete runs
    ``on_child_thread_ended``. Each archive is its own occurrence.
    """
    from plans.delegation_notice import _wake_parent

    for node in _live_parent_nodes(connection, thread_id):
        _wake_parent(
            connection, str(node["plan_id"]), thread_id,
            kind="child_thread_archived", subject_id=f"{thread_id}:{archived_at}",
            revision=0,
        )


def _what_became_of(connection, ended_id: str, workers: list[str]) -> str:
    """Say, for each worker the gone thread still delegated to, what became of it.

    Delete means delete (#2647), and neither case is adoptable. A worker
    deleted with its delegator never reaches here: its own delete ran this
    hook first and detached it (a step naming a thread keeps its row alive).
    One the delete promoted no longer names the gone thread and is a
    top-level agent. One still naming it was left behind by an older delete
    or a purge, so it is parentless and may need stopping.
    """
    from prompts import plan_wakes as text

    clauses = []
    for worker in workers:
        row = connection.execute(
            "SELECT spawned_by FROM threads WHERE id=?", (worker,),
        ).fetchone()
        template = (
            text.STRANDED_ORPHANED if row["spawned_by"] == ended_id
            else text.STRANDED_TOP_LEVEL
        )
        clauses.append(template.format(thread=worker))
    return text.WAKE_STRANDED_GRANDCHILDREN.format(workers="; ".join(clauses))


def on_child_thread_ended(connection, thread_id: str, *, reason: str) -> None:
    """F3: close a gone thread's open work, wake its parent, detach it, retire its plan.

    Order matters: leaves close first so the parent's later accept is not
    blocked on work that will never return, the wake goes out next while the
    parent node still names this worker, and detaching last frees the node
    for re-delegation. The thread's own plan is then archived, since nothing
    can act on it again (#2647). It runs only for a purge or a delete, never
    an archive (a user archive is ``on_child_thread_archived``). Idempotent --
    a second call finds nothing left to close, detach or archive, and the
    wake's own dedup key makes the repeat a no-op.

    A dying child may itself be a delegator. Its own leaf still names a
    grandchild, and ``archival._require_settled_spawned_work`` refuses to
    archive such a node -- which would raise straight out through thread
    delete and the retention sweep's purge. That guard protects delegated
    work someone can still settle, and nobody can once this thread is gone,
    so the leaf's attachment is released first, and the wake says what became
    of each such worker.
    """
    from plans import delegation, lifecycle
    from plans.delegation_notice import _wake_parent

    stranded = end_open_work(
        connection, thread_id, reason=f"Delegating thread {reason}",
        provenance=_ENDED,
    )
    live_nodes = _live_parent_nodes(connection, thread_id)
    stranded_note = (
        _what_became_of(connection, thread_id, stranded) if stranded else ""
    )
    for node in live_nodes:
        _wake_parent(
            connection, str(node["plan_id"]), thread_id,
            kind="child_thread_ended", subject_id=thread_id, revision=0,
            reason=reason, stranded_note=stranded_note,
        )
    for node in live_nodes:
        delegation.detach_spawned_thread(
            str(node["id"]), reason=reason, provenance=_ENDED, db=connection,
        )
    lifecycle.archive_ended_owner(
        thread_id, reason=reason, provenance=_ENDED, db=connection,
    )
