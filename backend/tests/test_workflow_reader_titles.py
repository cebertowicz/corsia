"""An ask read outside its plan tree names its subject: a workflow step names its run."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import seed_document
from infra.database import get_db
from plans import artifact_links, operator_chat_state, public, workflow_fields
from plans.boundary import PlanActor
from tests import test_workflow_runs as workflow_runs
from tests.plan_execution_helpers import open_turn
from tests.plan_writer_authorizer import raw_plan_seed
from tests.test_workflow_runs import Run, SHIP_BODY, SHIP_STEPS

pytestmark = pytest.mark.asyncio

_PERSON_FIRST = (
    "    - {id: sign, by: person, title: Sign the release form, check: Form signed,"
    " instruction: '## Review', difficulty: {default: routine}}\n"
    "    - {id: ship, required: true, check: Shipped, after: [sign], difficulty: {default: routine}}\n"
)


async def test_a_workflow_person_step_names_its_run_in_its_card_and_chat_row(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    reply = await run.start("ship", title="Ship the 0.4.1 notes")
    sign = reply["steps"][0]["id"]

    cards = run.rows("SELECT title FROM thread_updates WHERE node_id=? AND kind='person_step'", sign)
    assert cards == [{"title": "Ship the 0.4.1 notes — Sign the release form"}]

    db = get_db()
    try:
        states = operator_chat_state.read_thread_states(
            [run.thread_id], actor=PlanActor(None, "user-1", None, kind="user"), db=db,
        )["states"]
    finally:
        db.close()
    assert states[run.thread_id]["attention_items"] == [{
        "kind": "hold", "title": "Ship the 0.4.1 notes — Sign the release form", "node_id": sign,
    }]


async def test_a_workflow_person_steps_instructions_bar_names_its_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    reply = await run.start("ship", title="Ship the 0.4.1 notes")
    sign = reply["steps"][0]["id"]

    db = get_db()
    try:
        document = seed_document(db, run.thread_id, name="sign-instructions.md")
        artifact_links.link(sign, document, "input", actor_id=None, provenance="test", db=db)
        db.commit()
        bar = public.person_step_instructions(document, db=db)
    finally:
        db.close()
    assert bar["node_title"] == "Ship the 0.4.1 notes — Sign the release form"


async def test_a_plain_node_and_the_run_itself_keep_their_own_title(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    reply = await run.start("ship", title="Ship the 0.4.1 notes")
    plain = await run.update("add", {
        "title": "Book the venue", "acceptance_criteria": "Booked", "size": "S",
        "intent": "Book it",
    })

    db = get_db()
    try:
        titles = workflow_fields.reader_titles(
            db, [reply["node"]["id"], plain["node"]["id"], "no-such-node"],
        )
        empty = workflow_fields.reader_titles(db, [])
    finally:
        db.close()
    assert titles == {reply["node"]["id"]: "Ship the 0.4.1 notes", plain["node"]["id"]: "Book the venue"}
    assert empty == {}


async def test_a_nested_runs_step_and_a_moved_follow_up_still_name_the_outer_run(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {
        "outer": ("# Outer\n", "    - {id: design, workflow: inner, required: true, check: Design approved}\n"),
        "inner": ("# Inner\n", "    - {id: sketch, title: Sketch it, required: true, check: Sketch exists}\n"),
    })
    reply = await run.start("outer", title="Redesign the pane", difficulty="standard")
    design = reply["steps"][0]["id"]
    sketch = reply["steps"][0]["steps"][0]["id"]
    moved = (await run.update("add", {
        "title": "Sketch it again", "acceptance_criteria": "Sketch exists", "size": "S", "intent": "Redo the sketch",
    }))["node"]["id"]
    db = get_db()
    try:
        # A revision of the sketch where followups._unsettled_parent puts it once its run has settled: the top level.
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET workflow_ref='inner', workflow_step='sketch', follow_up_of=? WHERE id=?",
                (sketch, moved),
            )
        db.commit()
        titles = workflow_fields.reader_titles(db, [design, sketch, moved])
    finally:
        db.close()
    assert titles == {
        design: "Redesign the pane — design",
        sketch: "Redesign the pane — Sketch it",
        moved: "Redesign the pane — Sketch it again",
    }


async def test_a_run_under_an_ordinary_task_names_the_run_not_the_task(isolated_db, tmp_path, monkeypatch):
    # The step's commitment is the top-level task, where its cost rolls up; the run is the phase below it.
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    release = await run.update("add", {
        "kind": "phase", "title": "Release 0.4.1", "acceptance_criteria": "Released", "size": "L",
        "intent": "Ship the release",
    })
    reply = await run.start("ship", title="Ship the 0.4.1 notes", parent_id=release["node"]["id"])
    sign = reply["steps"][0]["id"]

    db = get_db()
    try:
        titles = workflow_fields.reader_titles(db, [sign])
    finally:
        db.close()
    assert titles == {sign: "Ship the 0.4.1 notes — Sign the release form"}


async def test_a_failed_step_names_its_run_and_every_plan_is_named_in_one_read(isolated_db, tmp_path, monkeypatch):
    # Only a failure assigned to the person asks them; a person step is theirs.
    failing = Run(tmp_path / "a", monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    signed = (await failing.start("ship", title="Ship the 0.4.1 notes"))["steps"][0]["id"]
    failed = await failing.update("fail", {"node_id": signed, "expected_revision": failing.revision(signed)})
    assert failed["node"]["state"] == "failed", failed
    # A second Run in the same database: one message index, and its own Turn id.
    monkeypatch.setattr(workflow_runs, "_init_messages_fts", lambda db: None)
    monkeypatch.setattr(workflow_runs, "open_turn", lambda db, thread_id: open_turn(db, thread_id, "turn-2"))
    asking = Run(tmp_path / "b", monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    sign = (await asking.start("ship", title="Ship the 0.5 notes"))["steps"][0]["id"]
    reads = []
    original = workflow_fields.reader_titles
    monkeypatch.setattr(workflow_fields, "reader_titles", lambda db, ids: reads.append(ids) or original(db, ids))

    db = get_db()
    try:
        states = operator_chat_state.read_thread_states(
            [failing.thread_id, asking.thread_id], actor=PlanActor(None, "user-1", None, kind="user"), db=db,
        )["states"]
    finally:
        db.close()
    assert states[failing.thread_id]["state"] == "failed"
    assert {"kind": "failed", "title": "Ship the 0.4.1 notes — Sign the release form", "node_id": signed} in (
        states[failing.thread_id]["attention_items"]
    )
    assert states[asking.thread_id]["attention_items"] == [{
        "kind": "hold", "title": "Ship the 0.5 notes — Sign the release form", "node_id": sign,
    }]
    assert len(reads) == 1
