"""Acceptance-gate handoff uses settled review outcomes."""

import pytest

from infra.database import get_db
from plans import gate_handoff, gate_handoff_record
from plans.gate_handoff_record import Run
from tests.test_workflow_gate_handoff import _answer, _go, _make, _offers

pytestmark = pytest.mark.asyncio


async def test_answered_but_unsettled_runs_do_not_offer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for _ in range(13):
        await _go(run, settle=False)
    assert not _offers()


async def test_any_rejected_round_makes_the_run_failed(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    _, gate = await _go(run, "changes_requested")
    _answer(gate, "approved")
    await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                   "evidence": "done", "verification_result": "passed"})
    db = get_db()
    try:
        (record,) = gate_handoff_record.gate_runs(db, ("", "gated", "gate"))
        assert gate_handoff_record.run_outcome(db, record) == "failed"
    finally:
        db.close()


async def test_thirteen_settled_clean_runs_offer(isolated_db, tmp_path, monkeypatch):
    run = _make(tmp_path, monkeypatch)
    for index in range(13):
        _, gate = await _go(run, settle=False)
        result = await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                                 "evidence": "done", "verification_result": "passed"})
        assert not isinstance(result, str), result
        assert len(_offers()) == (1 if index == 12 else 0)


def _record(details, document=None):
    return Run("p", "plan", "v", "start", "person", [], answers=details,
               settled_at="end", document_outcome=document)


@pytest.mark.parametrize(("detail", "document", "outcome"), [
    ({"verdict": "approved", "feedback_counts": {"comments": 1}}, None, "failed"),
    ({"verdict": "approved", "feedback_counts": {}}, None, "clean"),
    ({"verdict": "approved_with_changes"}, None, "failed"),
    ({"verdict": "approved", "went_ahead_unchanged": True}, {"went_ahead_unchanged": False, "changed_by": ["outside_edit"]}, "failed"),
    ({"verdict": "approved_with_changes"}, {"went_ahead_unchanged": True, "changed_by": []}, "clean"),
])
async def test_run_outcome(detail, document, outcome):
    assert gate_handoff_record.run_outcome(None, _record([detail], document)) == outcome


async def test_document_outcome_uses_first_ask_and_settlement_times(isolated_db, tmp_path, monkeypatch):
    from infra import document_went_ahead
    from plans import holds, service

    calls = []
    def fake(requests, db=None):
        import sqlite3

        from infra.config import DB_PATH

        # The history read runs Git, so it must hold no write: another writer gets the lock at once.
        probe = sqlite3.connect(str(DB_PATH), timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()
        calls.append(list(requests))
        return {request: {"went_ahead_unchanged": False, "changed_by": ["agent_revision"]} for request in requests}
    monkeypatch.setattr(document_went_ahead, "went_ahead_outcomes", fake)
    run = _make(tmp_path, monkeypatch)
    _, gate = await _go(run, None)
    db = get_db()
    try:
        holds.place_hold(gate, kind="blocked", reason_key="needs_review", reason="review", owner_kind="user",
                         owner_id="document-1", expected_revision=service._node_row(db, gate)["revision"],
                         actor_id=None, provenance="test.ask", db=db)
        hold = holds.active_holds(gate, db=db)[-1]["id"]
        first = db.execute("SELECT created_at FROM plan_node_events WHERE node_id=? AND event='hold_placed'"
                           " AND json_extract(payload_json,'$.hold_id')=?", (gate, hold)).fetchone()[0]
        holds.clear_write(gate, hold, expected_kind="blocked", actor_id=None, provenance="user.ask_cleared",
                          detail={"verdict": "approved", "feedback_counts": {"comments": 0}}, db=db)
        db.commit()
    finally:
        db.close()
    assert calls == []
    result = await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                             "evidence": "done", "verification_result": "passed"})
    assert not isinstance(result, str), result
    db = get_db()
    try:
        settled = db.execute("SELECT created_at FROM plan_node_events WHERE node_id=? AND event='state_transition'"
                             " AND new_state='accepted'", (gate,)).fetchone()[0]
        assert calls == [[("document-1", first, settled)]]
        (record,) = gate_handoff_record.gate_runs(db, ("", "gated", "gate"))
        assert gate_handoff_record.run_outcome(db, record) == "failed"
    finally:
        db.close()


async def test_reraised_ask_compares_to_first_presentation(isolated_db, tmp_path, monkeypatch):
    from infra import document_went_ahead
    from plans import holds, service

    run = _make(tmp_path, monkeypatch)
    _, gate = await _go(run, None)
    db = get_db()
    try:
        def ask():
            holds.place_hold(gate, kind="blocked", reason_key="needs_review", reason="review", owner_kind="user",
                             owner_id="document-1", expected_revision=service._node_row(db, gate)["revision"],
                             actor_id=None, provenance="test.ask", db=db)
            return holds.active_holds(gate, db=db)[-1]["id"]
        first_hold = ask()
        first = db.execute("SELECT created_at FROM plan_node_events WHERE node_id=? AND event='hold_placed'"
                           " AND json_extract(payload_json,'$.hold_id')=?", (gate, first_hold)).fetchone()[0]
        holds.clear_write(gate, first_hold, expected_kind="blocked", actor_id=None, provenance="test.replaced", db=db)
        # The agent revises the document before raising the next ask.
        revised_at = service._now()
        second_hold = ask()
        holds.clear_write(gate, second_hold, expected_kind="blocked", actor_id=None, provenance="user.ask_cleared",
                          detail={"verdict": "approved", "feedback_counts": {"comments": 0}}, db=db)
        db.commit()
    finally:
        db.close()
    calls = []
    def fake(requests, db=None):
        calls.append(list(requests))
        return {request: {"went_ahead_unchanged": request[1] > revised_at, "changed_by": ["agent_revision"]}
                for request in requests}
    monkeypatch.setattr(document_went_ahead, "went_ahead_outcomes", fake)
    result = await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                             "evidence": "done", "verification_result": "passed"})
    assert not isinstance(result, str), result
    assert calls[0][0][1] == first
    db = get_db()
    try:
        (record,) = gate_handoff_record.gate_runs(db, ("", "gated", "gate"))
        assert gate_handoff_record.run_outcome(db, record) == "failed"
    finally:
        db.close()


@pytest.mark.parametrize("typed_miss", [True, False])
async def test_scope_is_excluded_and_offer_lists_forgiven_and_scope(isolated_db, tmp_path, monkeypatch, typed_miss):
    from plans import decisions
    import json

    run = _make(tmp_path, monkeypatch)
    tiers = {}
    def triage(_db, records):
        for phase, record in records.items():
            record.tiers = tiers.get(phase, [])
    monkeypatch.setattr(gate_handoff_record, "_triage", triage)
    for index in range(14):
        reply = await run.start("gated")
        phase, gate = reply["node"]["id"], reply["steps"][1]["id"]
        if index == 0:
            tiers[phase] = ["instruction"]
        elif index == 1 and typed_miss:
            tiers[phase] = ["miss"]
        _answer(gate, "approved_with_changes" if index < 2 else "approved")
        result = await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                                 "evidence": "done", "verification_result": "passed"})
        assert not isinstance(result, str), result
        assert len(_offers()) == (1 if index == 13 else 0)
    db = get_db()
    try:
        decision_id = json.loads(_offers()[0]["payload_json"])["decision_id"]
        context = decisions.read(decision_id, db=db)["context"]
        assert ("needed a fix, and the next review needed none." in context) is typed_miss
        assert "not counted, because you changed what you wanted." in context
        count_line = context.splitlines()[0]
        assert count_line.startswith("In 13 of your last 13 reviews" if typed_miss else "In 12 of your last 13 reviews")
        assert count_line.endswith("the work needed no changes or only a fix that held." if typed_miss
                                   else "the work needed no changes.")
    finally:
        db.close()


async def test_replay_reads_kept_document_answers_and_never_git(monkeypatch):
    from infra import document_went_ahead
    from plans import gate_document_outcome

    def git(*_args, **_kwargs):
        raise AssertionError("a replay must not read the workspace history")
    monkeypatch.setattr(document_went_ahead, "went_ahead_outcomes", git)
    kept = {
        "unchanged": {"outcome": {"went_ahead_unchanged": True, "changed_by": []}},
        "edited": {"outcome": {"went_ahead_unchanged": False, "changed_by": ["outside_edit"]}},
        "no_document": {"outcome": None},
        "fault": {"fault": "history unavailable"},
    }
    reads = []
    def read_kept(_db, phases):
        reads.append(sorted(phases))
        return kept
    monkeypatch.setattr(gate_document_outcome, "kept", read_kept)
    records = {}
    for phase in (*kept, "never_kept"):
        record = _record([{"verdict": "approved", "feedback_counts": {}}])
        record.phase, record.artifact_id, record.first_round_at = phase, f"doc-{phase}", "first"
        records[phase] = record
    gate_handoff_record._documents(None, records)
    assert len(reads) == 1
    outcomes = {phase: gate_handoff_record.run_outcome(None, record) for phase, record in records.items()}
    assert outcomes == {"unchanged": "clean", "edited": "failed", "no_document": "clean",
                        "fault": None, "never_kept": None}


@pytest.mark.parametrize("failure", ["git_fault", "no_git"])
async def test_a_history_fault_counts_the_run_neither_way_and_settles(isolated_db, tmp_path, monkeypatch, failure):
    from infra import document_went_ahead
    from infra.guidance_states import GitFault
    from plans import gate_document_outcome, holds, service

    def fault(_requests, db=None):
        raise GitFault("git is busy") if failure == "git_fault" else FileNotFoundError("git")
    monkeypatch.setattr(document_went_ahead, "went_ahead_outcomes", fault)
    run = _make(tmp_path, monkeypatch)
    _, gate = await _go(run, None)
    db = get_db()
    try:
        holds.place_hold(gate, kind="blocked", reason_key="needs_review", reason="review", owner_kind="user",
                         owner_id="document-1", expected_revision=service._node_row(db, gate)["revision"],
                         actor_id=None, provenance="test.ask", db=db)
        hold = holds.active_holds(gate, db=db)[-1]["id"]
        holds.clear_write(gate, hold, expected_kind="blocked", actor_id=None, provenance="user.ask_cleared",
                          detail={"verdict": "approved", "feedback_counts": {"comments": 0}}, db=db)
        db.commit()
    finally:
        db.close()
    result = await run.update("mark_done", {"node_id": gate, "expected_revision": run.revision(gate),
                                             "evidence": "done", "verification_result": "passed"})
    assert not isinstance(result, str), result
    db = get_db()
    try:
        assert service._node_row(db, gate)["state"] == "accepted"
        (record,) = gate_handoff_record.gate_runs(db, ("", "gated", "gate"))
        assert record.document_unread
        assert "fault" in gate_document_outcome.kept(db, [record.phase])[record.phase]
        assert gate_handoff_record.run_outcome(db, record) is None
    finally:
        db.close()


@pytest.mark.parametrize(("tiers", "forgiven"), [([], False), (["miss"], True)])
async def test_miss_is_forgiven_only_by_next_counted_clean_run(tiers, forgiven):
    miss = _record([{"verdict": "approved_with_changes"}])
    miss.tiers = tiers
    clean = _record([{"verdict": "approved"}])
    state = gate_handoff.GateState()
    gate_handoff._apply_run(None, state, miss, True)
    gate_handoff._apply_run(None, state, clean, True)
    assert gate_handoff._window(state)[1] == ([miss] if forgiven else [])
    state.window.pop()
    gate_handoff._apply_run(None, state, miss, True)
    assert gate_handoff._window(state)[1] == []
