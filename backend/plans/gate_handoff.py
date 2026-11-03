"""Handing an acceptance gate off to an agent by its track record.

A gate is one workflow step marked ``gate: acceptance``; its key is the project of
the run owner's thread, the workflow and the step id. Its state is replayed from
events ``plans.gate_handoff_record`` reads; there is no table of counts.

The rule (how-workflows-work, "Handing off an acceptance gate"): a run counts once,
by the person's first answer at the gate, and only Approve is clean. After 8 clean
runs in a row on the current workflow version the person is offered four answers. A
correction hands the gate back, restarts the count and withdraws an open offer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import plans.service as service
from plans.gate_handoff_record import OFFER_EVENT, Run, gate_offers, gate_runs, latest_verdict, project_of

CLEAN_TO_OFFER = 8
#: The spot-check denominators, one per clean spot check in a row; after the last, none.
SPOT_DENOMINATORS = (5, 10, 20)
PROVENANCE = "plans.gate_handoff"
_REJECTED = "changes_requested"
#: The offer's option ids, in the order the person reads them, and what each hands the gate.
_HANDED = {"A": "spot", "B": "none", "C": "drop"}

OFFER_TITLE = "Hand the {step} review in {workflow} to an agent?"
OFFER_CONTEXT = "You approved {step} in {workflow} {count} times in a row with no changes."
OFFER_TRADEOFF = "Handing it off saves your review time, and any correction on it hands it back to you."
OFFER_RATIONALE = "Spot checks keep some review of it without asking you every time."
OFFER_OPTIONS = (
    "Hand it off, with spot checks",
    "Hand it off, with no spot checks",
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


def derive(db, key: tuple[str, str, str], version: str | None = None) -> GateState:
    """The gate's state, replayed from its events in time order, as of ``version`` when given."""
    items: list[tuple[str, int, str, Any]] = []
    runs = gate_runs(db, key)
    for run in runs:
        items.append((run.created, 0, "start", run))
        if run.answer is not None:
            items.append((run.answered_at or run.created, 2, "answer", run))
        items.extend((stamp, 3, "correction", run) for stamp in run.corrections)
    for offer in gate_offers(db, key, [run.phase for run in runs]):
        items.append((offer.raised, 1, "offer", offer))
        if offer.settled_at is not None:
            items.append((offer.settled_at, 1, "offer_answer", offer))
    state = GateState()
    current: str | None = None
    for _stamp, _order, name, data in sorted(items, key=lambda item: item[:2]):
        if name == "start" and data.version != current:
            current, state.streak, state.spot_clean = data.version, 0, 0
        elif name == "offer":
            state.open_offer = data.decision_id
        elif name == "offer_answer":
            if state.open_offer == data.decision_id:
                _apply_answer(state, data.option, data.settled_at)
            elif state.voided_offer == data.decision_id:
                state.voided_offer = None  # already withdrawn; nothing left to withdraw
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
    # Any answer that is not one of the three handing options keeps the review: Other, and an
    # offer withdrawn unanswered (the person's Won't do, its run closed), so it returns only
    # after another full streak.
    if option not in _HANDED:
        state.streak = 0
    else:
        state.kind, state.streak, state.spot_clean = _HANDED[option], 0, 0
        state.handed_on = datetime.fromisoformat(stamp).astimezone().date().isoformat()


def _hand_back(state: GateState) -> None:
    state.kind, state.streak, state.spot_clean = "person", 0, 0


def _apply_run(state: GateState, name: str, run: Run) -> None:
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
