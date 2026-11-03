"""Handing an acceptance gate off to an agent by its track record.

A gate is one workflow step marked ``gate: acceptance``; its key is the project of
the run owner's thread, the workflow and the step id. Everything here is read off
events that already exist (the run phases and their step nodes, the person's
``hold_cleared`` verdicts, ``correction_created`` and the offers this module
records as ``gate_handoff_offered``); there is no table of counts.

The rule (how-workflows-work, "Handing off an acceptance gate"): a run counts once,
by the person's first answer at the gate, and only Approve is clean. After 8 clean
runs in a row on the current workflow version the person is offered four answers. A
correction hands the gate back, restarts the count and withdraws an open offer.

Reading a gate replays its whole history, so the read is a fixed number of
statements whatever the gate's age: each one takes every run of the gate at
once (``json_each`` over the ids), never one statement per run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import plans.service as service

CLEAN_TO_OFFER = 8
#: The spot-check denominators, one per clean spot check in a row; after the last, none.
SPOT_DENOMINATORS = (5, 10, 20)
OFFER_EVENT = "gate_handoff_offered"
PROVENANCE = "plans.gate_handoff"
_ANSWERED_BY_PERSON = "user.ask_cleared"
_REJECTED = "changes_requested"
#: The offer's option ids, in the order the person reads them, and what each hands the gate.
_HANDED = {"A": "spot", "B": "none", "C": "drop"}

OFFER_TITLE = "Hand off the {step} review in {workflow}?"
OFFER_CONTEXT = "You approved {step} in {workflow} {count} runs in a row with no changes."
OFFER_TRADEOFF = "Handing it off saves your review time, and any correction on it hands it back to you."
OFFER_RATIONALE = "Spot checks keep some review of it without asking you every time."
OFFER_OPTIONS = (
    "Hand it off, with spot checks",
    "Hand it off, with no checks",
    "Drop the review",
    "Keep reviewing it myself",
)
OFFER_RECOMMENDATION = "A"
DROP_REASON = "dropped by the person on {date}"
GATE_REJECTED = "The person rejected this gate: ask for their review again, and close it only after a later answer."
GATE_NOTICE = "Gate step {step} was handed off by the person, so {how}."
GATE_HOW = {
    "agent": "a reviewer checks it instead of them",
    "spot": "it is a spot check, so the person still reviews it",
    "dropped": "it is already closed",
}


def _clean(verdict: str | None) -> bool:
    """Whether a first answer counts as clean: Approve only, never with changes or Reject."""
    return verdict == "approved"


@dataclass
class _Run:
    phase: str
    plan: str
    version: str | None
    created: str
    mode: str
    nodes: list[str]
    answer: str | None = None
    answered_at: str | None = None
    corrections: list[str] = field(default_factory=list)


@dataclass
class GateState:
    """Where a gate stands: ``kind`` is person, spot, none or drop.

    ``open_offer`` is the decision id of the offer the person can still answer;
    ``voided_offer`` is one a correction made void while it was still open.
    """

    kind: str = "person"
    streak: int = 0
    spot_clean: int = 0
    open_offer: str | None = None
    voided_offer: str | None = None
    handed_on: str | None = None


def project_of(db, plan_id: str) -> str:
    """The project of a plan's owner thread; a thread outside any project is the empty project."""
    row = db.execute(
        "SELECT t.project_id FROM plans p LEFT JOIN threads t ON t.id=p.owner_id"
        " WHERE p.id=? AND p.owner_kind='thread'", (plan_id,),
    ).fetchone()
    return (row["project_id"] or "") if row is not None else ""


def _ids(values) -> str:
    return json.dumps(list(values))


def _runs(db, key: tuple[str, str, str]) -> list[_Run]:
    """Each run of the gate in start order, with its first answer and corrections."""
    project, ref, step = key
    rows = db.execute(
        "SELECT p.id AS phase, p.plan_id AS plan, p.workflow_version AS version, p.created_at AS created, s.id AS node"
        " FROM plan_nodes p JOIN plans pl ON pl.id=p.plan_id AND pl.owner_kind='thread'"
        " LEFT JOIN threads t ON t.id=pl.owner_id"
        " JOIN plan_nodes s ON s.plan_id=p.plan_id AND s.parent_id=p.id AND s.workflow_step=?"
        " WHERE p.kind='phase' AND p.workflow_ref=? AND COALESCE(t.project_id,'')=?"
        " ORDER BY p.created_at,p.rowid,s.created_at,s.rowid",
        (step, ref, project),
    ).fetchall()
    created: dict[str, dict] = {}
    for row in db.execute(
        "SELECT node_id,payload_json FROM plan_node_events WHERE event='node_created'"
        " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,id",
        (_ids(row["node"] for row in rows),),
    ):
        created.setdefault(row["node_id"], json.loads(row["payload_json"] or "{}"))
    runs: dict[str, _Run] = {}
    for row in rows:
        run = runs.get(row["phase"])
        if run is None:
            first = created.get(row["node"], {})
            if first.get("workflow_gate") != "acceptance":
                continue
            run = runs[row["phase"]] = _Run(
                row["phase"], row["plan"], row["version"], row["created"], first.get("workflow_gate_mode", "person"), [],
            )
        run.nodes.append(row["node"])
    run_of = {node: run for run in runs.values() for node in run.nodes}
    for node, verdict, stamp in _answers(db, list(run_of), "ASC"):
        if run_of[node].answer is None:
            run_of[node].answer, run_of[node].answered_at = verdict, stamp
    for phase, stamp in _corrections(db, [(run.plan, run.phase) for run in runs.values()]):
        runs[phase].corrections.append(stamp)
    return list(runs.values())


def first_answer(db, node_ids: list[str]) -> tuple[str | None, str | None]:
    """The person's first verdict across a step's nodes, rounds included, and when; later answers never count.

    A run with no answer (not asked, or N/A) returns ``(None, None)``: it neither counts nor breaks a streak.
    """
    for _node, verdict, stamp in _answers(db, node_ids, "ASC"):
        return verdict, stamp
    return None, None


def latest_verdict(db, node_ids: list[str]) -> str | None:
    """The person's most recent verdict across a step's nodes, or None."""
    for _node, verdict, _stamp in _answers(db, node_ids, "DESC"):
        return verdict
    return None


def _answers(db, node_ids: list[str], order: str):
    """Each person's verdict on any of ``node_ids``, as (node, verdict, when), in one statement."""
    for row in db.execute(
        "SELECT node_id,payload_json,created_at FROM plan_node_events WHERE event='hold_cleared' AND provenance=?"
        f" AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at {order},rowid {order}",
        (_ANSWERED_BY_PERSON, _ids(node_ids)),
    ):
        verdict = (json.loads(row["payload_json"] or "{}").get("detail") or {}).get("verdict")
        if isinstance(verdict, str):
            yield row["node_id"], verdict, row["created_at"]


def _corrections(db, phases: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Each correction on work under these runs' phases, as (phase, when created).

    A correction is a follow-up of its source, so it is found by ``follow_up_of`` over each phase's subtree.
    """
    if not phases:
        return []
    return [(row["phase"], row["created_at"]) for row in db.execute(
        "WITH RECURSIVE below(id,plan,phase) AS ("
        " SELECT json_extract(value,'$[1]'),json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)"
        " UNION ALL SELECT n.id,below.plan,below.phase FROM below CROSS JOIN plan_nodes n"
        " ON n.plan_id=below.plan AND n.parent_id=below.id),"
        " fixes(id,phase) AS (SELECT n.id,below.phase FROM below CROSS JOIN plan_nodes n ON n.follow_up_of=below.id)"
        " SELECT fixes.phase,e.created_at FROM fixes CROSS JOIN plan_node_events e"
        " ON e.node_id=fixes.id AND e.event='correction_created' ORDER BY e.created_at,e.rowid",
        (json.dumps([list(pair) for pair in phases]),),
    )]


@dataclass
class _Offer:
    raised: str
    decision_id: str
    state: str
    option: str | None = None
    answered_at: str | None = None


def _offers(db, key: tuple[str, str, str], phases: list[str]) -> list[_Offer]:
    """Each offer of the gate with its decision's state and answer, in three statements.

    An offer is recorded on the run phase it was raised under, so it is read by those phases' ids.
    """
    from plans import decision_projection

    if not phases:
        return []
    raised = [
        (row["created_at"], payload["decision_id"]) for row in db.execute(
            "SELECT created_at,payload_json FROM plan_node_events WHERE event=?"
            " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,rowid",
            (OFFER_EVENT, _ids(phases)),
        ) if (payload := json.loads(row["payload_json"]))["key"] == list(key)
    ]
    if not raised:
        return []
    bodies = {}
    for row in db.execute(
        "SELECT external_id,metadata FROM artifact_identities WHERE integration='local' AND type=?"
        " AND state='active' AND external_id IN (SELECT value FROM json_each(?))",
        (decision_projection.DECISION_TYPE, _ids(decision for _stamp, decision in raised)),
    ):
        if (body := decision_projection._body(row)) is not None:
            bodies[row["external_id"]] = body
    answered_at = {row["node_id"]: row["created_at"] for row in db.execute(
        "SELECT node_id,created_at FROM plan_node_events WHERE event='decision_answered'"
        " AND node_id IN (SELECT value FROM json_each(?))",
        (_ids(body["node_id"] for body in bodies.values() if body["state"] == "answered"),),
    )}
    listed = []
    for stamp, decision in raised:
        body = bodies.get(decision)
        if body is None:
            continue
        offer = _Offer(stamp, decision, body["state"])
        if body["state"] == "answered":
            offer.option = (body.get("answer") or {}).get("option_id")
            offer.answered_at = answered_at.get(body["node_id"], stamp)
        listed.append(offer)
    return listed


def derive(db, key: tuple[str, str, str], version: str | None = None) -> GateState:
    """The gate's state, replayed from its events in time order, as of ``version`` when given."""
    items: list[tuple[str, int, str, Any]] = []
    runs = _runs(db, key)
    for run in runs:
        items.append((run.created, 0, "start", run))
        if run.answer is not None:
            items.append((run.answered_at or run.created, 2, "answer", run))
        items.extend((stamp, 3, "correction", run) for stamp in run.corrections)
    for offer in _offers(db, key, [run.phase for run in runs]):
        if offer.state == "invalidated":
            continue
        items.append((offer.raised, 1, "offer", offer))
        if offer.answered_at is not None:
            items.append((offer.answered_at, 1, "offer_answer", offer))
    state = GateState()
    current: str | None = None
    for _stamp, _order, name, data in sorted(items, key=lambda item: item[:2]):
        if name == "start" and data.version != current:
            current, state.streak, state.spot_clean = data.version, 0, 0
        elif name == "offer":
            state.open_offer = data.decision_id
        elif name == "offer_answer":
            if state.open_offer == data.decision_id:
                _apply_answer(state, data.option, data.answered_at)
        elif name == "correction":
            if state.open_offer is not None:
                state.voided_offer, state.open_offer = state.open_offer, None
            _hand_back(state)
        elif name != "start" and data.version == current:
            _apply_run(state, name, data)
    if version != current and current is not None:
        state.streak = state.spot_clean = 0
    return state


def _apply_answer(state: GateState, option: str | None, stamp: str) -> None:
    state.open_offer = None
    # Any answer that is not one of the three handing options, Other included, keeps the review.
    if option not in _HANDED:
        state.streak = 0
    else:
        state.kind, state.streak, state.spot_clean = _HANDED[option], 0, 0
        state.handed_on = datetime.fromisoformat(stamp).astimezone().date().isoformat()


def _hand_back(state: GateState) -> None:
    state.kind, state.streak, state.spot_clean = "person", 0, 0


def _apply_run(state: GateState, name: str, run: _Run) -> None:
    if run.mode == "spot":
        if _clean(run.answer):
            state.spot_clean += 1
        else:
            _hand_back(state)
    elif run.mode == "person" and state.kind == "person":
        state.streak = state.streak + 1 if _clean(run.answer) else 0


def mode_for(db, phase, step_id: str) -> tuple[str, GateState]:
    """How the next run's acceptance gate step is built: person, spot, agent or dropped.

    A spot check is drawn from the run phase's id, so reading it again gives the same answer.
    """
    key = (project_of(db, phase.plan_id), phase.workflow_ref, step_id)
    state = derive(db, key, phase.workflow_version)
    if state.kind == "person":
        return "person", state
    if state.kind == "drop":
        return "dropped", state
    if state.kind == "spot" and state.spot_clean < len(SPOT_DENOMINATORS):
        if drawn(phase.id, SPOT_DENOMINATORS[state.spot_clean]):
            return "spot", state
    return "agent", state


def drawn(phase_id: str, denominator: int) -> bool:
    """Whether the run starting under ``phase_id`` is a spot check at 1 in ``denominator``."""
    return int(hashlib.sha256(phase_id.encode()).hexdigest(), 16) % denominator == 0


def drop_reason(state: GateState) -> str:
    """The reason a dropped gate's step is closed with."""
    return DROP_REASON.format(date=state.handed_on)


def after_answer(db, node_id: str) -> None:
    """After the person's verdict is recorded on a step, offer a handoff if it made 8 clean in a row.

    Only an acceptance gate step counts; one offer at a time, and a gate already handed off is not offered again.
    """
    from plans import decisions
    from plans.workflow_fields import creation_payload

    node = service._node_row(db, node_id)
    if node["workflow_step"] is None or node["parent_id"] is None:
        return
    if creation_payload(db, node_id).get("workflow_gate") != "acceptance":
        return
    phase = service._node_row(db, node["parent_id"])
    key = (project_of(db, phase["plan_id"]), phase["workflow_ref"], node["workflow_step"])
    state = derive(db, key, phase["workflow_version"])
    if state.kind != "person" or state.open_offer or state.streak < CLEAN_TO_OFFER:
        return
    names = {"step": node["title"], "workflow": phase["title"]}
    decision = decisions.raise_offer(
        db, plan_id=phase["plan_id"], source_node_id=phase["id"], provenance=PROVENANCE,
        content={
            "title": OFFER_TITLE.format(**names), "context": OFFER_CONTEXT.format(count=state.streak, **names),
            "tradeoff": OFFER_TRADEOFF, "recommendation": OFFER_RECOMMENDATION, "rationale": OFFER_RATIONALE,
            "options": [{"id": option_id, "label": label} for option_id, label in zip("ABCD", OFFER_OPTIONS)],
            "other_enabled": True,
        },
    )
    service._event(
        db, phase["id"], OFFER_EVENT, actor_id=None, provenance=PROVENANCE,
        payload={"key": list(key), "version": phase["workflow_version"], "decision_id": decision["id"]},
    )


def after_correction(db, source_node_id: str) -> None:
    """After a correction on work under a run, withdraw any handoff offer it made void.

    The correction already hands each gate of that run back; an offer still open
    would let the person hand off a gate whose record just broke.
    """
    from plans import decisions
    from plans.workflow_fields import creation_payload

    for phase in db.execute(
        "WITH RECURSIVE up(id,parent) AS (SELECT id,parent_id FROM plan_nodes WHERE id=?"
        " UNION ALL SELECT n.id,n.parent_id FROM up JOIN plan_nodes n ON n.id=up.parent)"
        " SELECT n.* FROM up JOIN plan_nodes n ON n.id=up.id WHERE n.kind='phase' AND n.workflow_ref IS NOT NULL",
        (source_node_id,),
    ).fetchall():
        steps = {row["workflow_step"] for row in db.execute(
            "SELECT id,workflow_step FROM plan_nodes WHERE parent_id=? AND workflow_step IS NOT NULL", (phase["id"],),
        ) if creation_payload(db, row["id"]).get("workflow_gate") == "acceptance"}
        project = project_of(db, phase["plan_id"])
        for step in sorted(steps):
            state = derive(db, (project, phase["workflow_ref"], step), phase["workflow_version"])
            if state.voided_offer is not None:
                decisions.withdraw_offer(db, state.voided_offer, provenance=PROVENANCE)


def refuse_rejected(db, node_id: str, operation: str) -> None:
    """Refuse closing an acceptance gate step whose latest answer is Reject, until a later answer replaces it.

    No answer at all is not refused: some real workflows end a gate as N/A.
    """
    if operation not in {"mark_done", "accept", "accept_with_follow_up"}:
        return
    from plans.workflow_fields import creation_payload

    node = service._node_row(db, node_id)
    if node["workflow_step"] is None or creation_payload(db, node_id).get("workflow_gate") != "acceptance":
        return
    siblings = [row["id"] for row in db.execute(
        "SELECT id FROM plan_nodes WHERE parent_id IS ? AND workflow_step=?",
        (node["parent_id"], node["workflow_step"]),
    )]
    if latest_verdict(db, siblings) == _REJECTED:
        raise service.PlanError(GATE_REJECTED)
