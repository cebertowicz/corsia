"""An acceptance gate is handed off by its track record: 8 clean first answers, then the person's choice."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime

import pytest

from infra.database import get_db
from plans import decision_fields, decisions, gate_handoff, gate_handoff_record, holds, service
from plans.boundary import PlanActor
from plans.gate_handoff import GATE_HOW, GATE_NOTICE, GATE_REJECTED, SPOT_DENOMINATORS
from tests.test_workflow_runs import Run, SHIP_BODY
import tools.workflow_task_fields as workflow_task_fields

pytestmark = pytest.mark.asyncio

GATED = (
    "    - {id: work, check: Done, difficulty: {default: routine}}\n"
    "    - {id: gate, gate: acceptance, check: Approved, difficulty: {default: routine}}\n"
)
ALIGN = GATED.replace("acceptance", "alignment")
KEY = ("", "gated", "gate")


def _make(tmp_path, monkeypatch, steps=GATED, name="gated") -> Run:
    return Run(tmp_path, monkeypatch, {name: (SHIP_BODY, steps)})


def _run_db(sql, *args):
    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _answer(node: str, verdict: str) -> None:
    """The person's answer at a gate step: a user ask placed and cleared with a verdict, as the review bar does."""
    db = get_db()
    try:
        revision = service._node_row(db, node)["revision"]
        holds.place_hold(
            node, kind="blocked", reason_key="needs_review", reason="review", owner_kind="user", owner_id=None,
            expected_revision=revision, actor_id=None, provenance="test.ask", db=db,
        )
        hold = holds.active_holds(node, db=db)[-1]["id"]
        holds.clear_write(
            node, hold, expected_kind="blocked", actor_id=None, provenance="user.ask_cleared",
            detail={"verdict": verdict}, db=db,
        )
        db.commit()
    finally:
        db.close()


def _state(workflow="gated", step="gate"):
    db = get_db()
    try:
        version = _run_db("SELECT workflow_version FROM plan_nodes WHERE kind='phase' ORDER BY created_at DESC")[0]["workflow_version"]
        return gate_handoff.derive(db, ("", workflow, step), version)
    finally:
        db.close()


async def _go(run, verdict="approved", workflow="gated"):
    """One run of the gate: start it and give the gate step its answer (none when ``verdict`` is None)."""
    reply = await run.start(workflow)
    gate = reply["steps"][1]["id"]
    if verdict is not None:
        _answer(gate, verdict)
    return reply, gate


def _offers():
    return _run_db("SELECT payload_json FROM plan_node_events WHERE event='gate_handoff_offered'")


def _bump(version_note: str) -> None:
    """A new workflow version: commit an edit to the skill's definition."""
    workspace = workflow_task_fields.config.WORKSPACE
    sidecar = workspace / "skills" / "gated" / "corsia.yaml"
    sidecar.write_text(sidecar.read_text() + f"# {version_note}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", version_note],
        cwd=workspace, check=True,
    )


def _answer_offer(option: str) -> None:
    db = get_db()
    try:
        payload = json.loads(_offers()[-1]["payload_json"])
        decision = decisions.read(payload["decision_id"], db=db)
        decisions.answer(
            decision["id"], option_id=option, expected_revision=decision["revision"], mutation_id=f"offer-{option}-{decision['revision']}",
            actor=PlanActor(None, "user-1", None, kind="user"), provenance="api.plans.decision", db=db,
        )
        db.commit()
    finally:
        db.close()


def _correct(work: str) -> None:
    """A correction as ``add_correction`` makes one: a follow-up of its source, with the creation event."""
    from plans import followups

    db = get_db()
    try:
        correction = followups.add_follow_up(
            work, title="Fix", acceptance_criteria="Fixed", inherit_commitment=True, db=db,
        )
        service._event(db, correction.id, "correction_created", actor_id=None, provenance="plans.correction",
                       payload={"source_node_id": work})
        db.commit()
    finally:
        db.close()


async def _hand_off(run, option: str):
    for _ in range(8):
        await _go(run)
    _answer_offer(option)


async def test_eight_clean_runs_raise_exactly_one_offer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(7):
        await _go(run)
    assert _offers() == []
    await _go(run)
    await _go(run)
    await _go(run)
    (offer,) = _offers()
    payload = json.loads(offer["payload_json"])
    assert payload["key"] == list(KEY) and payload["decision_id"]
    db = get_db()
    try:
        decision = decisions.read(payload["decision_id"], db=db)
    finally:
        db.close()
    assert decision["state"] == "raised" and decision["thread_id"] == run.thread_id
    assert [option["label"] for option in decision["options"]] == list(gate_handoff.OFFER_OPTIONS)


@pytest.mark.parametrize("breaker", ["approved_with_changes", "changes_requested"])
async def test_anything_but_approve_breaks_the_streak(isolated_db, tmp_path, monkeypatch, breaker):
    run = _make(tmp_path, monkeypatch)
    for _ in range(7):
        await _go(run)
    await _go(run, breaker)
    assert _state().streak == 0
    for _ in range(7):
        await _go(run)
    assert _offers() == []
    await _go(run)
    assert len(_offers()) == 1


async def test_only_the_first_answer_counts(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(3):
        await _go(run)
    _reply, gate = await _go(run, "changes_requested")
    _answer(gate, "approved")
    assert _state().streak == 0
    db = get_db()
    try:
        assert gate_handoff_record.first_answer(db, [gate])[0] == "changes_requested"
    finally:
        db.close()


async def test_a_run_with_no_answer_neither_counts_nor_breaks(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _go(run)
    await _go(run, None)
    await _go(run)
    assert _state().streak == 2


async def test_alignment_gates_never_count_or_hand_off(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch, ALIGN)
    for _ in range(9):
        await _go(run)
    assert _offers() == []
    assert _run_db(
        "SELECT payload_json FROM plan_node_events WHERE event='node_created' AND payload_json LIKE '%workflow_gate_mode%'"
    ) == []


async def test_a_new_version_starts_the_count_again(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(5):
        await _go(run)
    _bump("v2")
    for _ in range(5):
        await _go(run)
    assert _offers() == [] and _state().streak == 5
    for _ in range(3):
        await _go(run)
    assert len(_offers()) == 1


async def test_keep_restarts_the_count_and_offers_again_after_eight_more(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "D")
    assert _state().kind == "person" and _state().streak == 0
    for _ in range(7):
        await _go(run)
    assert len(_offers()) == 1
    await _go(run)
    assert len(_offers()) == 2


async def test_no_second_offer_while_one_is_open(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(12):
        await _go(run)
    assert len(_offers()) == 1


async def test_none_builds_a_reviewer_step_and_never_a_spot_check(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "B")
    monkeypatch.setattr(gate_handoff, "drawn", lambda *_: pytest.fail("none never draws a spot check"))
    for _ in range(3):
        reply, gate = await _go(run, None)
        assert reply["steps"][1]["gate_mode"] == "agent" and reply["steps"][1]["by"] == "reviewer"
        assert _run_db("SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_created'", gate)[0][
            "payload_json"
        ].count('"workflow_gate_mode": "agent"') == 1
    assert GATE_NOTICE.format(step="gate", how=GATE_HOW["agent"]) in reply["notice"]


async def test_spot_checks_halve_then_stop_after_three_clean(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "A")
    seen = []
    monkeypatch.setattr(gate_handoff, "drawn", lambda _id, denominator: seen.append(denominator) or True)
    for _ in range(3):
        reply, gate = await _go(run)
        assert reply["steps"][1]["gate_mode"] == "spot"
    reply, _gate = await _go(run, None)
    assert seen == list(SPOT_DENOMINATORS) and reply["steps"][1]["gate_mode"] == "agent"
    assert _state().kind == "spot" and _state().spot_clean == 3


async def test_the_spot_check_draw_is_deterministic_per_run(isolated_db, tmp_path, monkeypatch):
    assert [gate_handoff.drawn("phase-x", 5) for _ in range(3)] == [gate_handoff.drawn("phase-x", 5)] * 3
    hits = sum(gate_handoff.drawn(f"phase-{n}", 5) for n in range(500))
    assert 60 < hits < 140


async def test_a_handed_off_gate_stays_handed_off_on_a_new_version_at_one_in_five(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "A")
    monkeypatch.setattr(gate_handoff, "drawn", lambda *_: True)
    await _go(run)
    await _go(run)
    assert _state().spot_clean == 2
    _bump("v2")
    seen = []
    monkeypatch.setattr(gate_handoff, "drawn", lambda _id, denominator: seen.append(denominator) or True)
    reply, _gate = await _go(run)
    assert seen == [5] and reply["steps"][1]["gate_mode"] == "spot"
    assert _state().kind == "spot"


async def test_a_spot_check_that_is_not_approved_hands_the_gate_back(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "A")
    monkeypatch.setattr(gate_handoff, "drawn", lambda *_: True)
    await _go(run, "approved_with_changes")
    state = _state()
    assert (state.kind, state.streak) == ("person", 0)
    reply, _gate = await _go(run)
    assert "gate_mode" not in reply["steps"][1]


async def test_a_failed_spot_check_answered_after_a_new_version_started_hands_the_gate_back(
    isolated_db, tmp_path, monkeypatch,
):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "A")
    monkeypatch.setattr(gate_handoff, "drawn", lambda *_: True)
    _reply, old_gate = await _go(run, None)
    _bump("v2")
    await _go(run, None)
    assert _state().kind == "spot"
    _answer(old_gate, "changes_requested")
    assert _state().kind == "person"


async def test_a_correction_hands_the_gate_back(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "B")
    reply, _gate = await _go(run, None)
    work = reply["steps"][0]["id"]
    _correct(work)
    assert _state().kind == "person"
    reply, _gate = await _go(run, None)
    assert "gate_mode" not in reply["steps"][1]


async def test_a_correction_withdraws_an_open_offer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(7):
        await _go(run)
    reply, gate = await _go(run)
    decision_id = json.loads(_offers()[-1]["payload_json"])["decision_id"]
    work = reply["steps"][0]["id"]
    await run.update("mark_done", {"node_id": work, "expected_revision": run.revision(work), "evidence": "d", "verification_result": "passed"})
    await run.update("add_correction", {
        "node_id": work, "expected_revision": run.revision(work), "title": "Fix", "acceptance_criteria": "Fixed",
        "affected_nodes": [{"node_id": gate, "expected_revision": run.revision(gate)}],
    })
    db = get_db()
    try:
        assert decisions.read(decision_id, db=db)["state"] == "invalidated"
    finally:
        db.close()
    assert _run_db("SELECT DISTINCT actor_id FROM plan_node_events WHERE event='decision_invalidated'") == [{"actor_id": None}]
    state = _state()
    assert (state.kind, state.streak, state.open_offer, state.voided_offer) == ("person", 0, None, None)
    for _ in range(8):
        await _go(run)
    assert len(_offers()) == 2


async def test_the_offer_names_the_step_and_workflow_by_title(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(8):
        reply, gate = await _go(run)
    (step,) = _run_db("SELECT title FROM plan_nodes WHERE id=?", gate)
    db = get_db()
    try:
        decision = decisions.read(json.loads(_offers()[-1]["payload_json"])["decision_id"], db=db)
    finally:
        db.close()
    assert decision["title"] == gate_handoff.OFFER_TITLE.format(step=step["title"], workflow=reply["node"]["title"])


async def test_a_gate_value_must_be_acceptance_or_alignment():
    from core.skill_workflow_validation import validate_workflow

    steps = [
        {"id": "a", "gate": "acceptance"}, {"id": "b", "gate": "alignment"}, {"id": "c", "gate": "aceptance"},
        {"id": "d", "gate": "acceptance", "workflow": "other"},
    ]
    assert validate_workflow({"workflow": {"steps": steps}}) == [
        "corsia.workflow.steps[2] 'c'.gate must be one of acceptance, alignment",
        "corsia.workflow.steps[3] 'd'.gate can't be set on a step that runs a workflow; set gate on that workflow's own steps",
    ]


async def test_a_correction_on_a_run_of_an_older_version_hands_the_gate_back(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "B")
    reply, _gate = await _go(run, None)
    _bump("v2")
    await _go(run, None)
    assert _state().kind == "none"
    _correct(reply["steps"][0]["id"])
    assert _state().kind == "person"


async def test_an_other_answer_keeps_the_review_and_restarts_the_count(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(8):
        await _go(run)
    payload = json.loads(_offers()[-1]["payload_json"])
    db = get_db()
    try:
        decision = decisions.read(payload["decision_id"], db=db)
        decisions.answer(
            decision["id"], option_id=decision_fields.OTHER_OPTION_ID, other_text="not yet", expected_revision=decision["revision"], mutation_id="offer-other",
            actor=PlanActor(None, "user-1", None, kind="user"), provenance="api.plans.decision", db=db,
        )
        db.commit()
    finally:
        db.close()
    assert _state().kind == "person" and _state().streak == 0
    await _go(run)
    assert len(_offers()) == 1


async def test_a_dismissed_offer_does_not_come_back_at_the_next_clean_run(isolated_db, tmp_path, monkeypatch):
    """The person's Won't do on the offer withdraws it; it counts as Keep, so it returns only after eight more."""
    from plans import decision_lifecycle

    run = _make(tmp_path, monkeypatch)
    for _ in range(8):
        await _go(run)
    payload = json.loads(_offers()[-1]["payload_json"])
    db = get_db()
    try:
        decision = decisions.read(payload["decision_id"], db=db)
        with service._write(db):
            decision_lifecycle.withdraw_for_review_node(
                db, decision["node_id"], actor_id="user-1", provenance="api.plans.user_settle",
            )
        db.commit()
        assert decisions.read(payload["decision_id"], db=db)["state"] == "invalidated"
    finally:
        db.close()
    assert _state().kind == "person" and _state().streak == 0 and _state().open_offer is None
    for _ in range(7):
        await _go(run)
    assert len(_offers()) == 1
    await _go(run)
    assert len(_offers()) == 2


async def test_drop_closes_the_gate_step_at_once_and_the_run_records_the_skip(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    await _hand_off(run, "C")
    reply = await run.start("gated")
    work, gate = (step["id"] for step in reply["steps"])
    assert reply["steps"][1]["gate_mode"] == "dropped"
    today = datetime.now().astimezone().date().isoformat()
    reason = f"Dropped by you on {today}"
    assert _run_db("SELECT state FROM plan_nodes WHERE id=?", gate) == [{"state": "closed"}]
    assert any(json.loads(row["payload_json"]).get("reason") == reason for row in _run_db(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='state_transition' AND new_state='closed'", gate,
    ))
    await run.update("mark_done", {"node_id": work, "expected_revision": run.revision(work), "evidence": "d", "verification_result": "passed"})
    (record,) = _run_db(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='workflow_run_ended'", reply["node"]["id"],
    )
    entry = json.loads(record["payload_json"])["steps"][1]
    assert (entry["state"], entry["skip_reason"], entry["gate_mode"]) == ("closed", reason, "dropped")


async def test_the_run_record_carries_each_gates_mode_and_first_answer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    reply, gate = await _go(run, "approved_with_changes")
    work = reply["steps"][0]["id"]
    await run.update("mark_done", {"node_id": work, "expected_revision": run.revision(work), "evidence": "d", "verification_result": "passed"})
    await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate), "evidence": "d", "verification_result": "passed"})
    (record,) = _run_db(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='workflow_run_ended'", reply["node"]["id"],
    )
    steps = json.loads(record["payload_json"])["steps"]
    assert [(entry["gate_mode"], entry["gate_answer"]) for entry in steps] == [
        (None, None), ("person", "approved_with_changes"),
    ]


async def test_reject_blocks_closing_until_a_later_answer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    _reply, gate = await _go(run, "changes_requested")
    args = {"node_id": gate, "expected_revision": run.revision(gate), "evidence": "d", "verification_result": "passed"}
    assert (await run.update("mark_done", args)).endswith(GATE_REJECTED)
    _answer(gate, "approved")
    args["expected_revision"] = run.revision(gate)
    assert not isinstance(await run.update("mark_done", args), str)


async def test_no_answer_at_all_does_not_block_closing(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    _reply, gate = await _go(run, None)
    reply = await run.update("mark_done", {
        "node_id": gate, "expected_revision": run.revision(gate), "evidence": "d", "verification_result": "passed",
    })
    assert not isinstance(reply, str)


async def test_the_offer_renders_as_the_owners_decision_and_the_person_can_answer_it(isolated_db, tmp_path, monkeypatch):
    from infra.auth import may_access_artifact
    from plans import attention_detail
    from registry import attention

    run = _make(tmp_path, monkeypatch)
    for _ in range(8):
        await _go(run)
    db = get_db()
    try:
        items = [item for item in attention.list_raised(db=db) if may_access_artifact(item["owner_thread"], "user-1", db=db)]
        (card,) = attention_detail.queue(attention_detail.annotate(items, db=db))
        assert card["renderer"] == "decision"
        assert card["detail"]["source"]["thread_id"] == run.thread_id
        assert card["detail"]["title"].startswith("Hand the ")
        authored = _run_db("SELECT DISTINCT actor_id FROM plan_node_events WHERE event IN ('decision_drafted','decision_raised')")
        assert authored == [{"actor_id": None}]
    finally:
        db.close()
    _answer_offer("A")
    (note,) = _run_db("SELECT thread_id,content FROM messages WHERE id LIKE 'plan-decision-answer:%'")
    assert note["thread_id"] == run.thread_id and "Hand it off, with spot checks" in note["content"]
    assert _state().kind == "spot"
