"""Pause is the user's (pause contract rule 1, #1969).

Only the user pauses or resumes. An agent that wants to come back to a task
later schedules it instead (``plans.scheduled_wait``), and a pause call tells
it so in one line.

What may start a paused agent's Turn (rules 2 and 3, #1971) lives here too:
people reach it, and anything automatic lands in its digest instead.
"""

from __future__ import annotations

import json

from plans.errors import PlanRefusal
from plans.scheduled_wait import pause_refusal

RESUME_REFUSAL = (
    "only the user resumes a paused plan. Tell the user it is paused and ask "
    "them to resume it."
)
NODE_RESUME_REFUSAL = (
    "The user paused this task; only they resume it. Pick up your next task."
)


def thread_paused_since(db, thread_id: str) -> str | None:
    """Return when the user paused this thread's plan, or ``None`` if it runs.

    Rule 5 (#1973): a Turn claiming inputs stamps this on each of them, so the
    line opening a paused agent's input is stored with the input and replays
    byte for byte on every harness (``core.message_history``).
    """
    return _paused_plan(db, thread_id)[1]


def _paused_plan(db, thread_id: str) -> tuple[str | None, str | None]:
    """Return this thread's plan and when the user paused it, if they did."""
    from plans.wait_causes import paused_since

    plan = db.execute(
        "SELECT id, state FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return None, None
    plan_id = str(plan["id"])
    return plan_id, paused_since([(None, plan)], db=db).get(plan_id)


#: Inputs that are not a person's message yet still reach a paused agent: the
#: user's decision answer. A scheduled wake is not one: only the agent's own
#: tool creates schedules and nothing records whether the user asked, so while
#: paused it lands in the digest (#2286 records that provenance).
_REACH_PAUSED_EVENTS = frozenset({"plan.decision_answered"})


def reaches_paused_agent(row) -> bool:
    """Whether this queued input may start a paused agent's Turn.

    The user's message or decision answer, another agent's message, and the
    continuation of a Turn a restart interrupted do. A worker's completion, a
    connected system's event, a scheduled wake and plan news do not.
    """
    metadata = json.loads(row["metadata"] or "{}")
    if not isinstance(metadata, dict):
        return False
    if row["role"] == "user":
        return not metadata.get("completion")
    return (
        metadata.get("event_type") in _REACH_PAUSED_EVENTS
        or "restart_registration_id" in metadata
    )


def set_aside_while_paused(db, thread_id: str, rows) -> bool:
    """Move a paused agent's automatic inputs into its digest; True if no Turn starts.

    Rule 2 (#1971): each event or worker completion ends here as a cancelled row
    that says why, plus one digest line the agent reads with its next Turn.
    Nothing is queued for later delivery, so a long pause never becomes a
    flood. Inputs that start no Turn (an already-built digest) stay queued.
    """
    from infra.passive_changes import append_change
    from infra.queued_input_settlement import cancel_queued
    from infra.thread_input_rows import starts_turn

    plan_id, since = _paused_plan(db, thread_id)
    if since is None or any(reaches_paused_agent(row) for row in rows):
        return False
    for row in rows:
        if not starts_turn(row):
            continue
        cancel_queued(db, thread_id, str(row["id"]), int(row["delivery_revision"]),
                      error="held_while_paused")
        append_change(
            db, object_kind="plan", object_id=str(plan_id), change_kind="held",
            summary=f"Arrived while paused: {_one_line(db, row)}",
            details={"message_id": str(row["id"]), "chip": None},
        )
    return True


def held_since(db, paused_at: dict[str, str]) -> set[str]:
    """The plans among ``paused_at`` that ``set_aside_while_paused`` held an input for
    since their pause, in one read.

    The journal stores naive UTC and plans store offset-aware UTC, so the two
    are compared to the second.
    """
    if not paused_at:
        return set()
    marks = ",".join("?" for _ in paused_at)
    return {
        str(row["object_id"])
        for row in db.execute(
            f"""SELECT object_id, MAX(substr(created_at,1,19)) AS last
                FROM context_change_journal
                WHERE object_kind='plan' AND change_kind='held'
                  AND object_id IN ({marks})
                GROUP BY object_id""",
            list(paused_at),
        ).fetchall()
        if str(row["last"]) >= paused_at[str(row["object_id"])][:19]
    }


def _one_line(db, row) -> str:
    """The held input's own summary, else the first line of what it said.

    A worker's completion names the worker, the first line of its result and
    where the whole result is: its row is cancelled, so this line is all the
    agent gets of it (#1971 review).
    """
    metadata = json.loads(row["metadata"] or "{}")
    metadata = metadata if isinstance(metadata, dict) else {}
    lines = [line.strip() for line in str(row["content"] or "").splitlines() if line.strip()]
    if metadata.get("completion") and metadata.get("from_thread"):
        from infra.thread_driver import PARENT_COMPLETION_HEADING

        # A Turn-end completion carries a heading line; an explicit
        # `delegate_send(intent="completion")` is the worker's text alone.
        if lines and lines[0] == PARENT_COMPLETION_HEADING:
            lines = lines[1:]
        worker = str(metadata["from_thread"])
        titled = db.execute("SELECT title FROM threads WHERE id=?", (worker,)).fetchone()
        name = (titled["title"] if titled is not None else None) or worker
        result = lines[0] if lines else "(no text)"
        return (f"worker {name} finished: {result[:160]}"
                f" (whole result: delegate_status(thread_id='{worker}'))")
    return (str(metadata.get("summary") or "").strip() or (lines or [""])[0])[:200]


def require(operation: str, params, actor) -> None:
    """Refuse an agent's pause or resume: both are the user's."""
    if actor.kind != "thread":
        return
    if operation == "resume":
        raise PlanRefusal(RESUME_REFUSAL)
    raise PlanRefusal(pause_refusal(params.get("node_id")))
