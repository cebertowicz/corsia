"""Work waits on the user only with an ask they can act on (plans-simplify, change 3).

A task held for the user with nothing to act on reads red while the user's
queue is empty, so the plan looks stalled and no one moves it. An agent's
``block_node`` on the user therefore names its ask. A question is a raised
decision, which already holds its source task (``plans.wait_causes``), so a
block beside one is refused. Something to read is a registered artifact the
plan's thread owns: the block links it to the task with the existing
``attention`` role and raises it in the user's one attention pile. When the
user clears that ask, the holds it placed clear and the agent is told
(``cleared``). ``shared/planning/plans-simplify-2026-09-29.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import plans.service as service

NO_ASK = (
    "work waits on the user only with something they can act on. For a "
    "question, raise a decision on this task: it holds the task by itself, so "
    "do not block it. For something to read, register the document and pass "
    "its artifact id as ask_artifact_id."
)
DECISION_HOLDS = (
    "this task has an open decision, which already holds it until the user "
    "answers; do not block it as well."
)
#: The user outcomes that end an ask, as the agent is told them. Defer keeps
#: the task held.
_ENDS_ASK = {
    "answer": "answered", "acknowledge": "acknowledged",
    "dismiss": "dismissed", "supersede": "replaced",
    "retire": "retired",
}
_PROVENANCE = "user.ask_cleared"
#: What an agent may ask the user to review: the viewers that carry a review bar.
ASKABLE_TYPES = ("document", "image")


@dataclass(frozen=True)
class AskRelease:
    """What the answering side hands Plans when it clears an ask, opaque to Plans.

    ``detail`` rides in the clearance event; ``text(ask, kind, title)`` is the notice the
    asking agent wakes on, given the artifact's title and the held task's kind and title.
    """

    detail: dict
    text: Callable[[str, str, str], str]


def raised_asks(db, artifact_id: str) -> list:
    """The user holds that wait on this artifact, newest first, with their asking agent."""
    return db.execute(
        """SELECT h.id AS hold_id, h.reason, h.created_at AS raised_at,
                  n.id AS node_id, p.owner_id AS asker_id, t.title AS asker_name
             FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id = l.node_id
             JOIN plans p ON p.id = n.plan_id
             JOIN plan_node_holds h ON h.node_id = n.id
             LEFT JOIN threads t ON t.id = p.owner_id AND p.owner_kind = 'thread'
            WHERE l.artifact_id=? AND l.role='attention'
              AND h.kind='blocked' AND h.owner_kind='user'
              AND h.owner_id=l.artifact_id AND h.cleared_at IS NULL
            ORDER BY h.created_at DESC, h.id DESC""",
        (artifact_id,),
    ).fetchall()


def is_user_block(operation: str, params) -> bool:
    """Whether this call holds a task for the user."""
    return operation == "block_node" and params.get("owner_kind") == "user"


def require(db, plan, node_id: str, params, actor) -> str | None:
    """Return the artifact an agent's user block asks about, or refuse the block.

    The user's own calls are unchanged: the rule is about what an agent may
    put in the user's queue.
    """
    from plans import artifact_links, decision_projection

    if actor.kind != "thread":
        return None
    if any(
        row["source_node_id"] == node_id
        and row["state"] == decision_projection.RAISED
        for row in decision_projection.rows_for_plans([plan["id"]], db=db)
    ):
        raise service.PlanInputError(DECISION_HOLDS)
    if params.get("condition") is not None:
        raise service.PlanInputError(
            "condition is not used for a user ask; the ask itself ends this hold"
        )
    if params.get("owner_id") is not None:
        raise service.PlanInputError(
            "owner_id is derived from ask_artifact_id for a user ask; omit owner_id"
        )
    ask = params.get("ask_artifact_id")
    if not isinstance(ask, str) or not ask.strip():
        raise service.PlanInputError(NO_ASK)
    known, owner = artifact_links.identity_owner(ask, db=db)
    if not known:
        raise service.PlanInputError(f"ask_artifact_id {ask!r} names no registered artifact")
    if owner != plan["owner_id"]:
        raise service.PlanInputError("ask_artifact_id must name an artifact this thread owns")
    identity = db.execute(
        "SELECT type,state FROM artifact_identities WHERE id=?", (ask.strip(),),
    ).fetchone()
    if identity["type"] == decision_projection.DECISION_TYPE:
        raise service.PlanInputError(
            "a decision is not blocked on; raise it, and it holds its task by itself"
        )
    if identity["type"] not in ASKABLE_TYPES or identity["state"] != "active":
        raise service.PlanInputError(
            "ask_artifact_id must name an active registered document or image"
        )
    return ask.strip()


def attach(db, node_id: str, artifact_id: str, *, actor_id: str | None, provenance: str) -> None:
    """Link the ask to its task and put it in the user's attention pile."""
    from plans import artifact_links
    from registry import attention

    artifact_links.link(
        node_id, artifact_id, "attention", actor_id=actor_id, provenance=provenance, db=db,
    )
    attention.raise_attention(artifact_id, db=db)


def cleared(artifact_id: str, outcome: str, *, db=None, release: AskRelease | None = None) -> int:
    """Release the tasks this ask held once its attention lifecycle ends.

    Returns how many holds cleared. Idempotent: a second clear finds none.
    ``defer`` remains open. Artifact retirement also ends the ask, but its
    notice must not claim that the user acted. ``release`` is the user's review
    answer as the answering side composed it: its payload rides in the clearance
    event and its text is the notice.
    """
    if outcome not in _ENDS_ASK:
        return 0
    with service._connection(db) as connection, service._write(connection):
        return _release(
            connection, artifact_id, _ENDS_ASK[outcome],
            retired=outcome == "retire", release=release,
        )


def _release(
    db, artifact_id: str, done: str, *, retired: bool = False, release: AskRelease | None = None,
) -> int:
    from plans import admission, holds, plan_wake
    from plans.plan_events import append_owner_event
    from plans.user_steering import CHANGE_KIND

    rows = db.execute(
        """SELECT h.id AS hold_id, n.id AS node_id, n.plan_id, n.kind, n.title,
                  i.title AS ask
             FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id = l.node_id
             JOIN plan_node_holds h ON h.node_id = n.id
             JOIN artifact_identities i ON i.id = l.artifact_id
            WHERE l.artifact_id=? AND l.role='attention'
              AND h.kind='blocked' AND h.owner_kind='user'
              AND h.owner_id=l.artifact_id AND h.cleared_at IS NULL
            ORDER BY h.created_at, h.id""",
        (artifact_id,),
    ).fetchall()
    for row in rows:
        holds.clear_write(
            row["node_id"], row["hold_id"], expected_kind="blocked",
            actor_id=None, provenance=_PROVENANCE, db=db,
            detail=release.detail if release else None,
        )
        cause = db.execute(
            """SELECT id, created_at FROM plan_node_events
               WHERE node_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (row["node_id"],),
        ).fetchone()
        ask = row["ask"] or "your ask"
        summary = (
            f'Your ask "{ask}" became unavailable, so {row["kind"]} '
            f'"{row["title"]}" no longer waits on it: pick it up.'
            if retired else
            release.text(ask, row["kind"], row["title"]) if release else
            f'The user {done} "{ask}", so {row["kind"]} '
            f'"{row["title"]}" no longer waits on them: pick it up.'
        )
        append_owner_event(
            db, plan_id=str(row["plan_id"]), event_id=str(cause["id"]),
            actor_id=None, event_type=CHANGE_KIND, summary=summary,
            details={"node_id": str(row["node_id"]), "action": "ask_cleared"},
            created_at=cause["created_at"],
        )
    for plan_id in dict.fromkeys(row["plan_id"] for row in rows):
        reconciled = admission.reconcile_plan(
            plan_id, actor_id=None, provenance=_PROVENANCE, db=db,
        )
        plan_wake.notify_selected(db, reconciled, plan_id=plan_id, causing_executor=None)
    return len(rows)
