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

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

import plans.service as service
from plans.node_states import open_user_block_sql

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
NOT_ASKABLE = (
    "ask_artifact_id must name an active registered document or image. For "
    "a mockup or other visuals, embed the rendered images in a markdown "
    "document as relative images (![caption](name.assets/screen.png)) and "
    "ask on that document."
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
        f"""SELECT h.id AS hold_id, h.reason, h.created_at AS raised_at,
                  n.id AS node_id, p.owner_id AS asker_id, t.title AS asker_name
             FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id = l.node_id
             JOIN plans p ON p.id = n.plan_id
             JOIN plan_node_holds h ON h.node_id = n.id
             LEFT JOIN threads t ON t.id = p.owner_id AND p.owner_kind = 'thread'
            WHERE l.artifact_id=? AND l.role='attention'
              AND {open_user_block_sql('h')} AND h.owner_id=l.artifact_id
            ORDER BY h.created_at DESC, h.id DESC""",
        (artifact_id,),
    ).fetchall()


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


def ask_holds(db, hold_ids) -> dict[str, dict]:
    """The user asks among ``hold_ids``, by hold id, read by primary key in two batches.

    Each is ``{artifact_id, open, raised_in, answer}``: the artifact asked about,
    whether the ask is still open, where the agent raised it (``turn_id``,
    ``tool_invocation_id``, ``provider_call_id``; empty for a hold placed before
    they were recorded) and what a clearing review recorded (``verdict``,
    ``feedback_counts``; empty when it recorded none). A hold that is not a user
    ask, or does not exist, is absent.
    """
    ids = list(dict.fromkeys(hold_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT h.id, h.node_id, h.owner_id, h.clearance_event_id,
                   {open_user_block_sql('h')} AS is_open
              FROM plan_node_holds h
             WHERE h.id IN ({marks}) AND h.kind='blocked' AND h.owner_kind='user'
               AND h.owner_id IS NOT NULL""",
        ids,
    ).fetchall()
    cleared = [r["clearance_event_id"] for r in rows if r["clearance_event_id"]]
    # holds.clear_write keeps the release detail under its own ``detail`` key.
    answers = {
        event_id: detail
        for event_id, payload in _event_payloads(db, cleared).items()
        if isinstance(detail := payload.get("detail"), dict)
    }
    placed = _placed_raised_in(db, [r["id"] for r in rows], {r["node_id"] for r in rows})
    return {
        r["id"]: {
            "artifact_id": r["owner_id"],
            "open": bool(r["is_open"]),
            "raised_in": placed.get(r["id"], {}),
            "answer": {
                k: v for k, v in answers.get(r["clearance_event_id"], {}).items()
                if k in ("verdict", "feedback_counts")
            },
        }
        for r in rows
    }


def _event_payloads(db, event_ids: list[str]) -> dict[str, dict]:
    if not event_ids:
        return {}
    marks = ",".join("?" * len(event_ids))
    found = {}
    for row in db.execute(
        f"SELECT id, payload_json FROM plan_node_events WHERE id IN ({marks})", event_ids,
    ):
        payload = _decoded(row["payload_json"])
        if payload is not None:
            found[row["id"]] = payload
    return found


def _decoded(raw) -> dict | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _placed_raised_in(db, hold_ids: list[str], node_ids) -> dict[str, dict]:
    """Where each hold was raised, from its ``hold_placed`` event, found by the task's event index."""
    if not node_ids:
        return {}
    marks = ",".join("?" * len(node_ids))
    wanted = set(hold_ids)
    found: dict[str, dict] = {}
    for row in db.execute(
        f"""SELECT payload_json FROM plan_node_events
             WHERE node_id IN ({marks}) AND event='hold_placed'""",
        sorted(node_ids),
    ):
        payload = _decoded(row["payload_json"])
        if payload is None or payload.get("hold_id") not in wanted:
            continue
        raised_in = payload.get("raised_in")
        found[payload["hold_id"]] = {
            k: v for k, v in raised_in.items() if isinstance(v, str) and v
        } if isinstance(raised_in, dict) else {}
    return found


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
    from plans import admission, admission_projection, holds, plan_wake
    from prompts import plan_wakes as wakes

    rows = db.execute(
        f"""SELECT h.id AS hold_id, n.id AS node_id, n.plan_id, n.kind, n.title,
                  n.revision, n.executor_thread_id, n.owner_thread_id,
                  p.owner_kind, p.owner_id, i.title AS ask
             FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id = l.node_id
             JOIN plans p ON p.id = n.plan_id
             JOIN plan_node_holds h ON h.node_id = n.id
             JOIN artifact_identities i ON i.id = l.artifact_id
            WHERE l.artifact_id=? AND l.role='attention'
              AND {open_user_block_sql('h')} AND h.owner_id=l.artifact_id
            ORDER BY h.created_at, h.id""",
        (artifact_id,),
    ).fetchall()
    released_by_plan: dict[str, list[tuple[sqlite3.Row, str]]] = {}
    for row in rows:
        holds.clear_write(
            row["node_id"], row["hold_id"], expected_kind="blocked",
            actor_id=None, provenance=_PROVENANCE, db=db,
            detail=release.detail if release else None,
        )
        ask = row["ask"] or "your ask"
        summary = (
            f'Your ask "{ask}" became unavailable, so {row["kind"]} '
            f'"{row["title"]}" no longer waits on it: pick it up.'
            if retired else
            release.text(ask, row["kind"], row["title"]) if release else
            f'The user {done} "{ask}", so {row["kind"]} '
            f'"{row["title"]}" no longer waits on them: pick it up.'
        )
        released_by_plan.setdefault(str(row["plan_id"]), []).append((row, summary))
    for plan_id, released in released_by_plan.items():
        summaries = {str(row["node_id"]): summary for row, summary in released}
        reconciled = admission.reconcile_plan(
            plan_id, actor_id=None, provenance=_PROVENANCE,
            selection_wake_summaries=summaries, db=db,
        )
        for row, summary in released:
            node_id = str(row["node_id"])
            executor = admission_projection.executor_of(row, row)
            if executor is None or node_id in reconciled.selection_woken:
                continue
            plan_wake.append(
                db, recipient=executor, kind=plan_wake.ASK_RELEASED, subject_id=node_id,
                revision=int(row["revision"]), plan_id=plan_id,
                content=wakes.WAKE_ASK_RELEASED.format(summary=summary), open_node_id=node_id,
            )
    return len(rows)
