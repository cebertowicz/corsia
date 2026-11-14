"""What happened at an acceptance gate, read off events that already exist.

The run phases and their step nodes, the person's verdicts, settlement times,
corrections and offers are read from existing records. Whether a run's document
went ahead unchanged is read once after its settlement and kept on the run
(``plans.gate_document_outcome``); triage classifies failed runs. There is no
table of counts: ``plans.gate_handoff`` replays the records into gate state.

Each read is a fixed number of statements whatever the gate's age: each one takes
every run of the gate at once (``json_each`` over the ids), never one statement per run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

OFFER_EVENT = "gate_handoff_offered"
_ANSWERED_BY_PERSON = "user.ask_cleared"
#: An offer's decision states that end it: answered, or withdrawn (the person's Won't do, a closed run, an archive).
_SETTLED = ("answered", "invalidated")


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
    answers: list[dict] = field(default_factory=list)
    title: str = ""
    tiers: list[str] = field(default_factory=list)
    settled_at: str | None = None
    artifact_id: str | None = None
    first_round_at: str | None = None
    answer_holds: set[str] = field(default_factory=set)
    document_outcome: dict | None = None
    #: The run asked on a document whose read is missing or failed: it counts neither way.
    document_unread: bool = False


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
    """Each run of the gate in start order, with answers, settlement and triage."""
    project, ref, step = key
    rows = db.execute(
        "SELECT p.id AS phase, p.plan_id AS plan, p.workflow_version AS version, p.created_at AS created, p.title AS title, s.id AS node"
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
                title=row["title"],
            )
        run.nodes.append(row["node"])
    run_of = {node: run for run in runs.values() for node in run.nodes}
    for node, verdict, stamp, detail, hold_id in _answers(db, list(run_of), "ASC"):
        run_of[node].answers.append(detail)
        if hold_id:
            run_of[node].answer_holds.add(hold_id)
        if run_of[node].answer is None:
            run_of[node].answer, run_of[node].answered_at = verdict, stamp
    for row in db.execute(
        "SELECT node_id,created_at,new_state FROM plan_node_events WHERE event='state_transition'"
        " AND new_state='accepted'"
        " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,rowid", (_ids(run_of),),
    ):
        run_of[row["node_id"]].settled_at = row["created_at"]
    first_shown: dict[tuple[str, str], str] = {}
    for row in db.execute(
        "SELECT node_id,payload_json,created_at FROM plan_node_events WHERE event='hold_placed'"
        " AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at,rowid", (_ids(run_of),),
    ):
        run = run_of[row["node_id"]]
        payload = json.loads(row["payload_json"] or "{}")
        artifact = payload.get("owner_id")
        if payload.get("owner_kind") == "user" and artifact:
            first_shown.setdefault((row["node_id"], artifact), row["created_at"])
        if run.first_round_at is None and payload.get("hold_id") in run.answer_holds:
            run.first_round_at = first_shown.get((row["node_id"], artifact), row["created_at"])
            run.artifact_id = artifact
    for phase, stamp in _corrections(db, [(run.plan, run.phase) for run in runs.values()]):
        runs[phase].corrections.append(stamp)
    _triage(db, runs)
    _documents(db, runs)
    return list(runs.values())


def run_outcome(db, run: Run) -> str | None:
    """Classify a person's entire review using batched answers read for this run."""
    answers = run.answers
    if not answers or run.settled_at is None or run.document_unread:
        return None
    if any(detail.get("verdict") == "changes_requested" for detail in answers):
        return "failed"
    go_ahead = next((detail for detail in reversed(answers) if detail.get("verdict") in ("approved", "approved_with_changes")), None)
    if go_ahead is not None and run.document_outcome is not None:
        return "clean" if run.document_outcome["went_ahead_unchanged"] else "failed"
    counts = answers[0].get("feedback_counts") or {}
    return "clean" if answers[0].get("verdict") == "approved" and all(
        counts.get(key, 0) == 0 for key in ("comments", "edits", "suggestions", "rejected_suggestions")
    ) else "failed"


def document_ask(run: Run) -> tuple[str, str, str] | None:
    """The document, first round and settlement time when the run's go-ahead is judged on a document.

    That is a run asked on a document, answered and settled; any other run gives None.
    """
    if run.artifact_id and run.first_round_at and run.settled_at and run.answers:
        return run.artifact_id, run.first_round_at, run.settled_at
    return None


def _documents(db, runs: dict[str, Run]) -> None:
    """Attach each run's kept document read, in one statement and with no Git.

    A read that found no recorded document leaves the outcome None, so the run is
    judged by its feedback counts; a read that failed, or was never kept, counts
    the run neither way rather than guess it clean.
    """
    from plans import gate_document_outcome

    asked = {phase: run for phase, run in runs.items() if document_ask(run)}
    if not asked:
        return
    kept = gate_document_outcome.kept(db, asked)
    for phase, run in asked.items():
        answer = kept.get(phase)
        if answer is None or "fault" in answer:
            run.document_unread = True
        else:
            run.document_outcome = answer.get("outcome")


def _triage(db, runs: dict[str, Run]) -> None:
    """Attach triage to runs through the canonical correction attribution in bounded reads."""
    if not runs:
        return
    from core.correction_attribution import attributed_facts
    from core.correction_learning import TRIAGE_EVENT
    from core.corrections_recording import subject_key

    members: dict[str, list[str]] = {}
    for row in db.execute(
        "WITH RECURSIVE below(id,plan,phase) AS ("
        " SELECT json_extract(value,'$[1]'),json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?)"
        " UNION ALL SELECT n.id,below.plan,below.phase FROM below CROSS JOIN plan_nodes n"
        " ON n.plan_id=below.plan AND n.parent_id=below.id) SELECT id,phase FROM below",
        (json.dumps([[run.plan, run.phase] for run in runs.values()]),),
    ):
        members.setdefault(row["id"], []).append(row["phase"])
    facts = attributed_facts(db, node_ids=members)
    subjects: dict[str, list[str]] = {}
    for fact in facts:
        key = subject_key(fact.source_kind, fact.source_id)
        subjects.setdefault(key, []).extend(members.get(fact.node_id or "", []))
    for row in db.execute(
        "SELECT subject_key,attributes FROM outcome_events WHERE event_name=? AND retracted_at IS NULL"
        " AND subject_key IN (SELECT value FROM json_each(?))", (TRIAGE_EVENT, _ids(subjects)),
    ):
        tier = json.loads(row["attributes"]).get("tier")
        if tier:
            for phase in subjects[row["subject_key"]]:
                runs[phase].tiers.append(tier)


def first_answer(db, node_ids: list[str]) -> tuple[str | None, str | None]:
    """The person's first verdict across a step's nodes, rounds included, and when; later answers never count.

    A run with no answer (not asked, or N/A) returns ``(None, None)``.
    """
    for _node, verdict, stamp, _detail, _hold_id in _answers(db, node_ids, "ASC"):
        return verdict, stamp
    return None, None


def latest_verdict(db, node_ids: list[str]) -> str | None:
    """The person's most recent verdict across a step's nodes, or None."""
    for _node, verdict, _stamp, _detail, _hold_id in _answers(db, node_ids, "DESC"):
        return verdict
    return None


def _answers(db, node_ids: list[str], order: str):
    """Each person's verdict and hold on the nodes, in one statement."""
    for row in db.execute(
        "SELECT node_id,payload_json,created_at FROM plan_node_events WHERE event='hold_cleared' AND provenance=?"
        f" AND node_id IN (SELECT value FROM json_each(?)) ORDER BY created_at {order},rowid {order}",
        (_ANSWERED_BY_PERSON, _ids(node_ids)),
    ):
        payload = json.loads(row["payload_json"] or "{}")
        detail = payload.get("detail") or {}
        verdict = detail.get("verdict")
        if isinstance(verdict, str):
            yield row["node_id"], verdict, row["created_at"], detail, payload.get("hold_id")


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
    """One offer: ``settled_at`` is when the person answered it or it was withdrawn; ``option`` is None when withdrawn."""

    raised: str
    decision_id: str
    state: str
    option: str | None = None
    settled_at: str | None = None


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
    settled = {row["node_id"]: row["created_at"] for row in db.execute(
        "SELECT node_id,created_at FROM plan_node_events WHERE event IN ('decision_answered','decision_invalidated')"
        " AND node_id IN (SELECT value FROM json_each(?))",
        (_ids(body["node_id"] for body in bodies.values() if body["state"] in _SETTLED),),
    )}
    listed = []
    for stamp, decision in raised:
        body = bodies.get(decision)
        if body is None:
            continue
        offer = Offer(stamp, decision, body["state"])
        if body["state"] in _SETTLED:
            offer.option = (body.get("answer") or {}).get("option_id")
            offer.settled_at = settled.get(body["node_id"], stamp)
        listed.append(offer)
    return listed
