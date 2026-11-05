"""An ask read outside its plan tree names its subject: a workflow step names its run."""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import operator_chat_state, workflow_fields
from plans.boundary import PlanActor
from tests.test_workflow_runs import Run, SHIP_BODY

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


async def test_a_plain_node_and_the_run_itself_keep_their_own_title(isolated_db, tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, {"ship": (SHIP_BODY, _PERSON_FIRST)})
    reply = await run.start("ship", title="Ship the 0.4.1 notes")
    plain = await run.update("add", {
        "title": "Book the venue", "acceptance_criteria": "Booked", "size": "S",
        "intent": "Book it", "intent_words": [
            {"author": "person", "quote": "Ship the release notes.", "role": "authority"},
        ],
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
