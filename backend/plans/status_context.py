"""The thin plan projection a checkpoint or context assembly delivers: at most 2 KiB.

At the context boundary a thread needs to know, without rereading its plan:
which plan and revision this is, its state and progress, the current task and
its revision, and only the next actionable step or the genuine wait -- plus
the read that gets the exact detail. An idle or paused plan says so; nothing
here guesses a current step. Every node, completed history, descriptions and
acceptance bodies stay out: they are one ``plan_read`` away.

Plans owns these facts and hands them to the owner thread through the
passive digest (``infra.passive_changes``): a journal row when the projection
changes, collapsed to the latest, delivered as one line at the thread's next
Turn boundary. It is never part of the cached system prefix and never
memoized, so a plan write costs no prefix rebuild.

A plan write does not compute the projection under the writer lock: it marks
the plan dirty in its own transaction (``mark_touched``), and
``plans.status_drain`` journals the projection after commit, or under the lock
at the latest before a digest is built for the owner. The projection shows the
plan as of its last write, computed when the status is drained.
"""

from __future__ import annotations

import json
from typing import Any

from plans import not_moving, operator_nodes, service, status
from prompts.plan_tools import DELEGATED_PLAN_GUIDANCE

#: Engineering budget from the brief: the checkpoint plan projection is at
#: most 2 KiB of serialized UTF-8.
CHECKPOINT_BYTES = 2048
#: The journal change kind the owner's passive digest carries the projection under.
CHANGE_KIND = "plan.status"
#: The one oversize budget, owned by the projection the pane reads too.
OPEN_NODE_BUDGET = operator_nodes.OPEN_NODE_BUDGET


def projection(db, plan_row, *, actor, clock: str | None = None) -> dict[str, Any]:
    """Return the thin projection, fitted to ``CHECKPOINT_BYTES``."""
    return _project(db, plan_row, actor=actor, clock=clock or service._now())


def mark_touched(db, *, since: str) -> list[str]:
    """Mark every thread plan this transaction wrote as owing its status.

    Called by ``plan_write.transaction`` inside the write's own transaction,
    so the plan write and the marker that its status is owed commit together:
    a process exit after the commit leaves the marker for the startup sweep or
    the owner's next digest, never a lost status. The projection itself is
    computed and journaled by ``plans.status_drain``, off the writer lock.

    This runs under the writer lock on every plan write, so it finds the
    touched plans with one read per table (``touched_plans``). Returns every
    touched plan id, scope-owned plans included, for the post-commit nudge.
    """
    from plans import status_drain

    rows = touched_plans(db, since=since)
    status_drain.mark(db, [str(row["id"]) for row in rows if row["owner_kind"] == "thread"])
    return sorted(str(row["id"]) for row in rows)


def touched_plans(db, *, since: str, owner_id: str | None = None) -> list:
    """The plan rows written since ``since``, optionally only one thread's.

    A plan is touched when its row, any of its nodes, any node event, or any
    node attempt's selection or release was written since the transaction
    began -- reconciliation selects and releases through
    ``plan_node_attempts`` alone, with no matching node touch of its own when
    the mutation that made the task eligible already touched it, so a bare
    reconciliation still counts as a touch. Each table is read once rather
    than probing every plan: a per-plan correlated probe scanned
    ``plan_node_attempts`` once per plan and held the writer lock for seconds
    on a large database.
    """
    owner = "" if owner_id is None else " AND p.owner_kind='thread' AND p.owner_id=?"
    return db.execute(
        """SELECT p.* FROM plans p WHERE p.id IN (
             SELECT id FROM plans WHERE updated_at>=?
             UNION SELECT plan_id FROM plan_nodes WHERE updated_at>=?
             UNION SELECT n.plan_id FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
                   WHERE e.created_at>=?
             UNION SELECT plan_id FROM plan_node_attempts
                   WHERE selected_at>=? OR released_at>=?)""" + owner,
        (since, since, since, since, since) + (() if owner_id is None else (owner_id,)),
    ).fetchall()


def journal_status(db, plan_row) -> None:
    """Compute and journal the owner's thin plan status now, on ``db``.

    The projection reaches the owner thread the way every other context
    change does: as one line of the passive digest at its next Turn boundary
    -- never in the cached system prefix, so a plan write costs no prefix
    rebuild. Changes to one plan collapse to the latest, so a Turn that wrote
    five times hands over one projection, and a projection that says what the
    last one said appends nothing.
    """
    computed = compute_status(db, plan_row)
    if computed is not None:
        append_status(db, str(plan_row["id"]), *computed)


def compute_status(
    db, plan_row, *, owner: tuple[str, str | None] | None = None,
) -> tuple[str, int] | None:
    """The owner's status line and the plan revision it shows; None without an owner thread.

    ``owner`` is the owner thread's ``(user_id, project_id)`` when the caller
    already read it; otherwise it is read here.
    """
    if plan_row["owner_kind"] != "thread":
        return None
    from plans.boundary import PlanActor

    if owner is None:
        thread = db.execute(
            "SELECT user_id,project_id FROM threads WHERE id=?", (plan_row["owner_id"],),
        ).fetchone()
        if thread is None:
            return None
        owner = (str(thread["user_id"]), thread["project_id"])
    actor = PlanActor(str(plan_row["owner_id"]), *owner)
    thin = projection(db, plan_row, actor=actor)
    return "Current plan status: " + render(thin), int(thin["plan"]["revision"])


def append_status(db, plan_id: str, summary: str, revision: int) -> None:
    """Journal one computed status unless it says what the last one said."""
    from infra.passive_changes import append_change

    last = db.execute(
        """SELECT summary FROM context_change_journal
           WHERE object_kind='plan' AND object_id=? AND change_kind=?
           ORDER BY seq DESC LIMIT 1""",
        (plan_id, CHANGE_KIND),
    ).fetchone()
    if last is not None and _same_facts(last["summary"], summary):
        return
    append_change(
        db,
        object_kind="plan",
        object_id=plan_id,
        change_kind=CHANGE_KIND,
        summary=summary,
        # The plan pane already shows this. It is state for the agent alone, so
        # the transcript gets no chip row for it.
        details={
            "path": CHANGE_KIND,
            "plan_revision": revision,
            "chip": None,
        },
        actor_thread_id=None,
    )


def _project(db, plan_row, *, actor, clock: str) -> dict[str, Any]:
    # The thin status shows no cost, and a digest may build it under the writer
    # lock, where the cost roll-up held that lock for seconds (#2489). It reads the
    # open work, so a write to a plan with long history owes the same short digest.
    facts = status._facts(db, plan_row, actor, clock)
    overview = status._overview(db, plan_row, facts)
    thin: dict[str, Any] = {
        "plan": overview["plan"],
        "progress": overview["progress"],
        "severity": overview["severity"],
        "current_task": overview["current_task"],
        "no_current_task": overview["no_current_task"],
        "next_step": overview["next_step"],
        "as_of": clock,
        "status_read": "plan_read()",
    }
    if plan_row["parent_plan_id"] is not None:
        from plans import projections

        if projections.live_delegation(
            db, str(plan_row["owner_id"]), str(plan_row["parent_plan_id"]),
        ):
            thin["delegated_plan_guidance"] = DELEGATED_PLAN_GUIDANCE
    oversize = operator_nodes.oversize(facts["nodes"])
    if oversize is not None:
        thin["oversize"] = oversize
    if overview["severity"] == "not_moving":
        thin.update(_nothing_ready(facts, overview["severity"], str(plan_row["owner_id"])))
        thin["note"] = not_moving.note(facts["stuck"])
    elif plan_row["state"] == "paused":
        thin["note"] = "plan paused"
    elif overview["no_current_task"] and overview["next_step"] is None:
        thin.update(_nothing_ready(facts, overview["severity"], str(plan_row["owner_id"])))
    return _fit(thin)


#: What open work is doing when the actor has no current task and nothing
#: ready, by the severity the same read computed; "idle" only when nothing is open.
_NOTHING_READY = {
    "needs_input": "nothing ready: open work needs the user",
    "blocked": "nothing ready: open work is blocked",
    "moving": "nothing ready: open work is moving or in review",
    "waiting": "nothing ready: open work is waiting",
}


#: The one open step is a returned step the plan's owner has to judge (#3116).
_OWES_VERDICT = (
    "nothing ready: you owe the verdict on open_step; accept it,"
    " accept_with_follow_up for what is left, or reject it"
)


def _owes_verdict(node: dict, owner_id: str) -> bool:
    review = node.get("review") or {}
    return (
        node["state"] == "review_requested"
        and review.get("kind") == "parent_agent"
        and (review.get("actor") or {}).get("id") == owner_id
    )


def _nothing_ready(facts: dict, severity: str, owner_id: str) -> dict[str, Any]:
    """Name the open work that explains an empty current and next (#2023)."""
    open_leaves = [n for n in facts["leaves"] if n["state"] not in service._TERMINAL_STATES]
    if not open_leaves:
        return {"note": "idle: nothing open"}
    next_ids = set(facts["next_ids"])
    first = min(open_leaves, key=lambda node: status._rank(node, next_ids))  # min keeps plan order on a tie
    note = _OWES_VERDICT if _owes_verdict(first, owner_id) else _NOTHING_READY.get(
        severity, "nothing ready",
    )
    return {"note": note, "open_step": status._brief(first, facts)}


def render(projection_: dict[str, Any]) -> str:
    """The exact text a context owner delivers: compact JSON, nothing invented."""
    return json.dumps(projection_, sort_keys=True, separators=(",", ":"))


def _fit(thin: dict[str, Any]) -> dict[str, Any]:
    """Drop the least necessary facts, in order, until the budget holds."""
    for drop in ("open_step", "next_step", "current_task"):
        if len(render(thin).encode()) <= CHECKPOINT_BYTES:
            return thin
        if thin.get(drop) is not None:
            thin[drop] = {"id": thin[drop]["id"], "detail_read": thin[drop]["detail_read"]}
    return thin


def _same_facts(previous: str, current: str) -> bool:
    """Whether two journaled projections say the same thing, ``as_of`` aside."""
    def facts(text: str) -> str:
        body = text.split("Current plan status: ", 1)[-1]
        try:
            loaded = json.loads(body)
        except ValueError:
            return text
        loaded.pop("as_of", None)
        return json.dumps(loaded, sort_keys=True)
    return facts(previous) == facts(current)
