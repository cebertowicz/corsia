"""Handing an acceptance gate off to an agent by its track record.

A gate is one workflow step marked ``gate: acceptance``; its key is the project of
the run owner's thread, the workflow and the step id. Everything here is read off
events that already exist (the run phases and their step nodes, the person's
``hold_cleared`` verdicts, ``correction_created`` and the offers this module
records as ``gate_handoff_offered``); there is no table of counts.

The rule (how-workflows-work, "Handing off an acceptance gate"): a run counts once,
by the person's first answer at the gate, and only Approve is clean. After 8 clean
runs in a row on the current workflow version the person is offered four answers. A
correction hands the gate back and restarts the count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
_KEEP = "D"
_PACIFIC = ZoneInfo("America/Los_Angeles")

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
    version: str | None
    created: str
    mode: str
    nodes: list[str]
    answer: str | None = None
    answered_at: str | None = None
    corrections: list[str] = field(default_factory=list)


@dataclass
class GateState:
    """Where a gate stands: ``kind`` is person, spot, none or drop."""

    kind: str = "person"
    streak: int = 0
    spot_clean: int = 0
    open_offer: bool = False
    dropped_on: str | None = None


def project_of(db, plan_id: str) -> str:
    """The project of a plan's owner thread; a thread outside any project is the empty project."""
    row = db.execute(
        "SELECT t.project_id FROM plans p LEFT JOIN threads t ON t.id=p.owner_id"
        " WHERE p.id=? AND p.owner_kind='thread'", (plan_id,),
    ).fetchone()
    return (row["project_id"] or "") if row is not None else ""


def _runs(db, key: tuple[str, str, str]) -> list[_Run]:
    """Each run of the gate in start order, with its first answer and corrections."""
    from plans.workflow_fields import creation_payload

    project, ref, step = key
    rows = db.execute(
        "SELECT p.id AS phase, p.workflow_version AS version, p.created_at AS created, s.id AS node"
        " FROM plan_nodes p JOIN plans pl ON pl.id=p.plan_id AND pl.owner_kind='thread'"
        " LEFT JOIN threads t ON t.id=pl.owner_id"
        " JOIN plan_nodes s ON s.parent_id=p.id AND s.workflow_step=?"
        " WHERE p.kind='phase' AND p.workflow_ref=? AND COALESCE(t.project_id,'')=?"
        " ORDER BY p.created_at,p.rowid,s.created_at,s.rowid",
        (step, ref, project),
    ).fetchall()
    runs: dict[str, _Run] = {}
    for row in rows:
        run = runs.get(row["phase"])
        if run is None:
            created = creation_payload(db, row["node"])
            if created.get("workflow_gate") != "acceptance":
                continue
            run = runs[row["phase"]] = _Run(
                row["phase"], row["version"], row["created"], created.get("workflow_gate_mode", "person"), [],
            )
        run.nodes.append(row["node"])
    for run in runs.values():
        run.answer, run.answered_at = first_answer(db, run.nodes)
        run.corrections = _corrections(db, run.phase)
    return list(runs.values())


def first_answer(db, node_ids: list[str]) -> tuple[str | None, str | None]:
    """The person's first verdict across a step's nodes, rounds included, and when; later answers never count.

    A run with no answer (not asked, or N/A) returns ``(None, None)``: it neither counts nor breaks a streak.
    """
    for verdict, stamp in _answers(db, node_ids, "ASC"):
        return verdict, stamp
    return None, None


def latest_verdict(db, node_ids: list[str]) -> str | None:
    """The person's most recent verdict across a step's nodes, or None."""
    for verdict, _stamp in _answers(db, node_ids, "DESC"):
        return verdict
    return None


def _answers(db, node_ids: list[str], order: str):
    marks = ",".join("?" * len(node_ids))
    for row in db.execute(
        f"SELECT payload_json,created_at FROM plan_node_events WHERE event='hold_cleared' AND provenance=?"
        f" AND node_id IN ({marks}) ORDER BY created_at {order},rowid {order}",
        (_ANSWERED_BY_PERSON, *node_ids),
    ):
        verdict = (json.loads(row["payload_json"] or "{}").get("detail") or {}).get("verdict")
        if isinstance(verdict, str):
            yield verdict, row["created_at"]


def _corrections(db, phase_id: str) -> list[str]:
    """When each correction on work under this run's phase was created."""
    return [row["created_at"] for row in db.execute(
        "WITH RECURSIVE below(id) AS (SELECT ? UNION ALL SELECT n.id FROM plan_nodes n JOIN below ON n.parent_id=below.id)"
        " SELECT created_at FROM plan_node_events WHERE event='correction_created'"
        " AND json_extract(payload_json,'$.source_node_id') IN (SELECT id FROM below) ORDER BY created_at,rowid",
        (phase_id,),
    )]


def _offers(db, key: tuple[str, str, str]) -> list[tuple[str, str, tuple[str | None, str] | None]]:
    """Each offer of the gate: when raised, its decision's state, and its answer (option id, answer time)."""
    from plans import decision_projection

    project, ref, step = key
    listed = []
    for row in db.execute(
        "SELECT created_at,payload_json FROM plan_node_events WHERE event=?"
        " AND json_extract(payload_json,'$.key[1]')=? AND json_extract(payload_json,'$.key[2]')=?"
        " ORDER BY created_at,rowid", (OFFER_EVENT, ref, step),
    ):
        payload = json.loads(row["payload_json"])
        if payload["key"][0] != project:
            continue
        decision = decision_projection.read(payload["decision_id"], db=db)
        answered = None
        if decision["state"] == "answered":
            event = db.execute(
                "SELECT created_at FROM plan_node_events WHERE node_id=? AND event='decision_answered'",
                (decision["node_id"],),
            ).fetchone()
            answered = (decision["answer"].get("option_id"), event["created_at"])
        listed.append((row["created_at"], decision["state"], answered))
    return listed


def derive(db, key: tuple[str, str, str], version: str | None = None) -> GateState:
    """The gate's state, replayed from its events in time order, as of ``version`` when given."""
    items: list[tuple[str, int, str, Any]] = []
    for run in _runs(db, key):
        items.append((run.created, 0, "start", run))
        if run.answer is not None:
            items.append((run.answered_at or run.created, 2, "answer", run))
        items.extend((stamp, 3, "correction", run) for stamp in run.corrections)
    for raised, status, answered in _offers(db, key):
        items.append((raised, 1, "offer", status))
        if answered is not None:
            items.append((answered[1], 1, "offer_answer", answered))
        elif status == "invalidated":
            items.append((raised, 1, "offer_answer", (None, raised)))
    state = GateState()
    current: str | None = None
    for _stamp, _order, name, data in sorted(items, key=lambda item: item[:2]):
        if name == "start" and data.version != current:
            current, state.streak, state.spot_clean = data.version, 0, 0
        elif name == "offer":
            state.open_offer = True
        elif name == "offer_answer":
            _apply_answer(state, data[0], data[1])
        elif name != "start" and data.version == current:
            _apply_run(state, name, data)
    if version != current and current is not None:
        state.streak = state.spot_clean = 0
    return state


def _apply_answer(state: GateState, option: str | None, stamp: str) -> None:
    state.open_offer = False
    if option == _KEEP:
        state.streak = 0
    elif option in _HANDED:
        state.kind, state.streak, state.spot_clean = _HANDED[option], 0, 0
        state.dropped_on = datetime.fromisoformat(stamp).astimezone(_PACIFIC).date().isoformat()


def _hand_back(state: GateState) -> None:
    state.kind, state.streak, state.spot_clean = "person", 0, 0


def _apply_run(state: GateState, name: str, run: _Run) -> None:
    if name == "correction":
        _hand_back(state)
    elif run.mode == "spot":
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
    return DROP_REASON.format(date=state.dropped_on)


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
    names = {"step": node["workflow_step"], "workflow": phase["workflow_ref"]}
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
