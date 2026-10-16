"""Work waits on the user only with an ask they can act on (plans-simplify, change 3).

A task held for the user with nothing to act on reads red while the user's
queue is empty, so the plan looks stalled and no one moves it. An agent's
``block_node`` on the user therefore names its ask. A real tradeoff is a raised
decision, which already holds its source task (``plans.wait_causes``), so a
block beside one is refused. Something to review or approve is a registered
artifact the plan's thread owns: the block links it to the task with the existing
``attention`` role and raises it in the user's one attention pile. When the
user clears that ask, the holds it placed clear and the agent is told
(``cleared``). ``shared/planning/plans-simplify-2026-09-29.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

import plans.ask_endings as ask_endings
import plans.service as service
from plans.node_states import open_user_block_sql

NO_ASK = (
    "work waits on the user only with something they can act on. For "
    "something to review or approve, register the document or image and pass "
    "its artifact id as ask_artifact_id. For a real tradeoff with nothing to "
    "review, raise a decision on this task: it holds the task by itself, so do "
    "not block it. Ask anything else in chat."
)
DECISION_HOLDS = (
    "this task has an open decision, which already holds it until the user "
    "answers; do not block it as well."
)
NOT_ASKABLE = (
    "ask_artifact_id must name an active registered document or image. For "
    "a mockup or other visuals, embed the rendered images in a markdown "
    "document as relative images (![caption](name.assets/screen.png)) and "
    "ask on that document."
)
#: The user outcomes that end an ask, as the agent is told them. Defer keeps
#: the task held.
_ENDS_ASK = {
    "answer": ("resolved_by_user", "answered"), "acknowledge": ("resolved_by_user", "acknowledged"),
    "dismiss": ("resolved_by_user", "dismissed"), "supersede": ("replaced", "replaced"),
    "retire": ("retired", "retired"), "reject_with_reason": ("rejected", "rejected"),
    "request_revision": ("revision_requested", "asked for a revision of"),
    "answer_internally": ("answered_internally", "answered"),
}
ASK_ENDINGS = ask_endings.ASK_ENDINGS
_PROVENANCE = "user.ask_cleared"
#: What an agent may ask the user to review: the viewers that carry a review bar.
ASKABLE_TYPES = ("document", "image")


def askable_sql(alias: str) -> str:
    """Whether the artifact row ``alias`` is a type a review request can name."""
    return f"{alias}.type IN ({','.join(repr(kind) for kind in ASKABLE_TYPES)})"


def ask_linked_sql(alias: str) -> str:
    """Whether the artifact row ``alias`` is askable and linked to a task for attention.

    Such an artifact's raised state belongs to its open requests
    (``open_asks_sql``), never to its registry metadata.
    """
    return (
        f"({askable_sql(alias)} AND EXISTS (SELECT 1 FROM plan_node_artifact_links al"
        f" WHERE al.artifact_id={alias}.id AND al.role='attention'))"
    )


@dataclass(frozen=True)
class AskRelease:
    """What the answering side hands Plans when it clears an ask, opaque to Plans.

    ``detail`` rides in the clearance event. Plans owns the one durable wake
    summary, so every ask answer names the task, answer, and remaining holds.
    """

    detail: dict


#: Which open review requests ``open_asks_sql`` selects: ``?`` binds the
#: artifact or the task; ``all`` binds nothing.
_OPEN_ASKS_BY = {"artifact": " AND l.artifact_id=?", "node": " AND h.node_id=?", "all": ""}


def open_asks_sql(columns: str, *, joins: str = "", by: str = "artifact") -> str:
    """The one definition of open review requests: a SELECT of ``columns``.

    A user block owned by the artifact on a task linked to it for attention.
    It has no task-state filter: every way a task ends withdraws its requests
    (``withdraw_node_asks``), so an open one always has a live task to release.
    A deleted or purged owner's open tasks are closed before its plan is
    archived (``delegation_child_lifecycle.end_open_work``), and migration 120
    withdraws what older archives left open.
    ``by`` picks one document's, one task's or every request (``_OPEN_ASKS_BY``);
    ``l``, ``n``, ``p`` and ``h`` name the link, task, plan and hold. Each scope
    reads ``idx_plan_holds_active_node``, never the clearance history.
    """
    return (
        f"SELECT {columns} FROM plan_node_artifact_links l"
        " JOIN plan_nodes n ON n.id = l.node_id"
        " JOIN plans p ON p.id = n.plan_id"
        " JOIN plan_node_holds h ON h.node_id = n.id"
        f" {joins} WHERE l.role='attention'"
        f" AND {open_user_block_sql('h')} AND h.owner_id=l.artifact_id{_OPEN_ASKS_BY[by]}"
    )


def raised_asks(db, artifact_id: str) -> list:
    """The user holds that wait on this artifact, newest first, with their asking agent."""
    return db.execute(
        open_asks_sql(
            "h.id AS hold_id, h.reason, h.created_at AS raised_at,"
            " n.id AS node_id, p.owner_id AS asker_id, t.title AS asker_name",
            joins="LEFT JOIN threads t ON t.id = p.owner_id AND p.owner_kind = 'thread'",
        ) + " ORDER BY h.created_at DESC, h.id DESC",
        (artifact_id,),
    ).fetchall()


def open_ask_artifacts(db, artifact_id: str | None = None) -> list[dict]:
    """The open review requests, or one document's, oldest first, as ``open_asks_sql`` defines them.

    Registry callers use this solely to derive their presentation; they never
    own or mutate a hold.
    """
    sql = open_asks_sql(
        "l.artifact_id, h.id AS hold_id", by="all" if artifact_id is None else "artifact",
    ) + " ORDER BY h.created_at, h.id"
    return [dict(row) for row in db.execute(sql, () if artifact_id is None else (artifact_id,))]


def deferral_stamp(db, artifact_id: str) -> dict[str, str]:
    """The metadata stamp that ties a deferral to the open request it defers.

    A document holds at most one open request; with none, the deferral stays
    non-ask attention and is stamped with nothing.
    """
    holds = [str(ask["hold_id"]) for ask in open_ask_artifacts(db, artifact_id)]
    return {"deferred_hold_id": holds[0]} if len(holds) == 1 else {}


def is_user_block(operation: str, params) -> bool:
    """Whether this call holds a task for the user."""
    return operation == "block_node" and params.get("owner_kind") == "user"


def placed_ask_hold_sql(event_id_expr: str) -> str:
    """The hold id the ``hold_placed`` event ``event_id_expr`` placed, as an SQL scalar.

    Read by the event's primary key, so no history scan stands behind it.
    """
    return (
        "(SELECT json_extract(placed.payload_json, '$.hold_id') FROM plan_node_events placed"
        f" WHERE placed.id = {event_id_expr})"
    )


def user_ask_hold_sql(event_id_expr: str, column: str) -> str:
    """One ``column`` (``id`` or ``owner_id``) of the user ask the event ``event_id_expr`` placed.

    Open or since cleared; NULL when the event placed no ask (a hold with no
    artifact, or another owner's).
    """
    if column not in ("id", "owner_id"):
        raise ValueError(f"not an ask column: {column}")
    return (
        f"(SELECT ask.{column} FROM plan_node_holds ask"
        f" WHERE ask.id = {placed_ask_hold_sql(event_id_expr)} AND ask.kind='blocked'"
        " AND ask.owner_kind='user' AND ask.owner_id IS NOT NULL)"
    )



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
        raise service.PlanInputError(NOT_ASKABLE)
    return ask.strip()


def attach(db, node_id: str, artifact_id: str, *, actor_id: str | None, provenance: str) -> None:
    """Link an ask to its task; its open hold determines pile visibility."""
    from plans import artifact_links

    artifact_links.link(
        node_id, artifact_id, "attention", actor_id=actor_id, provenance=provenance, db=db,
    )


def withdraw_node_asks(
    db, node_id: str, *, actor_id: str | None, provenance: str,
) -> int:
    """Withdraw the open asks a terminal or archived task no longer owns."""
    from plans import holds

    rows = db.execute(
        open_asks_sql("h.id AS hold_id", by="node") + " ORDER BY h.created_at, h.id",
        (node_id,),
    ).fetchall()
    for row in rows:
        holds.clear_write(
            node_id, str(row["hold_id"]), expected_kind="blocked", actor_id=actor_id,
            provenance=provenance, detail=ask_endings.detail(why="task_ended", actor_id=actor_id), db=db,
        )
    return len(rows)


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
        why, wake_verb = _ENDS_ASK[outcome]
        return _release(
            connection, artifact_id, why, wake_verb=wake_verb,
            retired=outcome == "retire", release=release,
        )


def _release(
    db, artifact_id: str, why: str, *, wake_verb: str, retired: bool = False,
    release: AskRelease | None = None,
) -> int:
    from plans import admission_projection, holds, plan_wake
    from plans.plan_events import append_owner_event
    from plans.user_steering import CHANGE_KIND
    from prompts import plan_wakes as wakes

    rows = db.execute(
        open_asks_sql(
            "h.id AS hold_id, n.id AS node_id, n.plan_id, n.kind, n.title,"
            " n.revision, n.executor_thread_id, n.owner_thread_id,"
            " p.owner_kind, p.owner_id,"
            " EXISTS (SELECT 1 FROM plan_node_holds other"
            " WHERE other.node_id=n.id AND other.id<>h.id"
            " AND other.cleared_at IS NULL) AS other_holds,"
            " i.title AS ask",
            joins="JOIN artifact_identities i ON i.id = l.artifact_id",
        ) + " ORDER BY h.created_at, h.id",
        (artifact_id,),
    ).fetchall()
    detail = dict(release.detail) if release is not None else {}
    detail.update(ask_endings.detail(why=why))
    causes = [
        holds.clear_write(
            row["node_id"], row["hold_id"], expected_kind="blocked",
            actor_id=None, provenance=_PROVENANCE, db=db,
            detail=detail or None,
        )
        for row in rows
    ]
    executors = {row["hold_id"]: admission_projection.executor_of(row, row) for row in rows}
    handoffs = _reconcile_for_askers(db, rows, executors)
    for row, (cause_id, cause_at) in zip(rows, causes):
        summary = _release_summary(row, wake_verb, retired=retired, release=release)
        executor = executors[row["hold_id"]]
        wake_id = None if executor is None else plan_wake.append(
            db,
            recipient=executor,
            kind=plan_wake.ASK_RELEASED,
            subject_id=str(row["node_id"]),
            revision=int(row["revision"]),
            about=str(row["hold_id"]),
            plan_id=str(row["plan_id"]),
            content=plan_wake.with_next(
                db, str(row["plan_id"]), executor,
                wakes.WAKE_ASK_RELEASED.format(summary=summary),
                **handoffs.pop((str(row["plan_id"]), executor), {}),
            ),
            open_node_id=str(row["node_id"]),
        )
        append_owner_event(
            db,
            plan_id=str(row["plan_id"]),
            event_id=cause_id,
            actor_id=None,
            event_type=CHANGE_KIND,
            summary=summary,
            details={
                "node_id": str(row["node_id"]),
                "action": "ask_cleared",
                "ask_wake_id": wake_id,
            },
            created_at=cause_at,
        )
    if rows:
        from plans import notifications

        notifications.report_touched({str(row["plan_id"]) for row in rows}, db=db)
    return len(rows)


def _reconcile_for_askers(db, rows, executors: dict) -> dict[tuple[str, str], dict]:
    """Reconcile each plan an answer released, and return the askers' handoff facts.

    The ask wake is the asker's result of this write, so reconcile is told not
    to wake the asker again (``causing_executor``), and runs before the wake so
    its next block is the state the asker returns to. Standing in for the
    work-selected wake, the asker's first wake on each plan also carries what
    that wake would have: its facts, keyed by plan and asker (``selected_fact``).
    """
    from plans import admission, plan_wake

    handoffs: dict[tuple[str, str], dict] = {}
    for plan_id in dict.fromkeys(str(row["plan_id"]) for row in rows):
        causing = next(
            executors[row["hold_id"]] for row in rows if str(row["plan_id"]) == plan_id
        )
        reconciled = admission.reconcile_plan(
            plan_id, actor_id=None, provenance=_PROVENANCE, causing_executor=causing, db=db,
        )
        if causing is not None:
            handoffs[(plan_id, causing)] = plan_wake.selected_fact(db, reconciled, causing)
    return handoffs


def _release_summary(row, wake_verb: str, *, retired: bool, release: AskRelease | None) -> str:
    """Return the one-line answer the wake and its receipt carry."""
    from prompts import plan_wakes as wakes

    detail = release.detail if release is not None else {}
    return wakes.ask_release_summary(
        ask=str(row["ask"] or "your ask"), done=wake_verb, retired=retired,
        kind=str(row["kind"]), title=str(row["title"]), node_id=str(row["node_id"]),
        verdict=detail.get("verdict"), note=detail.get("note"),
        other_holds=bool(row["other_holds"]),
    )
