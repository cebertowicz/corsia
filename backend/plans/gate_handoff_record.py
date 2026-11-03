"""What happened at an acceptance gate, read off events that already exist.

The run phases and their step nodes, the person's ``hold_cleared`` verdicts,
``correction_created`` and the offers ``plans.gate_handoff`` records as
``gate_handoff_offered``; there is no table of counts. ``plans.gate_handoff``
replays them into a gate's state.

Each read is a fixed number of statements whatever the gate's age: each one takes
every run of the gate at once (``json_each`` over the ids), never one statement per run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

OFFER_EVENT = "gate_handoff_offered"
_ANSWERED_BY_PERSON = "user.ask_cleared"


@dataclass
class Run:
    phase: str
    plan: str
    version: str | None
    created: str
    mode: str
    nodes: list[str]
    answer: str | None = None
    answered_at: str | None = None
    corrections: list[str] = field(default_factory=list)


def project_of(db, plan_id: str) -> str:
    """The project of a plan's owner thread; a thread outside any project is the empty project."""
    row = db.execute(
        "SELECT t.project_id FROM plans p LEFT JOIN threads t ON t.id=p.owner_id"
        " WHERE p.id=? AND p.owner_kind='thread'", (plan_id,),
    ).fetchone()
    return (row["project_id"] or "") if row is not None else ""


def _ids(values) -> str:
    return json.dumps(list(values))


def gate_runs(db, key: tuple[str, str, str]) -> list[Run]:
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
    runs: dict[str, Run] = {}
    for row in rows:
        run = runs.get(row["phase"])
        if run is None:
            first = created.get(row["node"], {})
            if first.get("workflow_gate") != "acceptance":
                continue
            run = runs[row["phase"]] = Run(
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
class Offer:
    raised: str
    decision_id: str
    state: str
    option: str | None = None
    answered_at: str | None = None


def gate_offers(db, key: tuple[str, str, str], phases: list[str]) -> list[Offer]:
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
        offer = Offer(stamp, decision, body["state"])
        if body["state"] == "answered":
            offer.option = (body.get("answer") or {}).get("option_id")
            offer.answered_at = answered_at.get(body["node_id"], stamp)
        listed.append(offer)
    return listed
