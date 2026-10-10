"""The CC event pump must survive foreign residue on the shared pooled connection.

Production wires the pump with ``db=None`` (``cc_runtime.py`` spawn path,
``cc_runtime_resume.py`` resume path), so every persistence call inside a handler
independently claims the OUTERMOST transaction on the event-loop thread's shared
pooled connection. Those claims are guarded, correctly, and the guard raising
inside a handler kills the reader task -- which kills claude and ends the turn
with ``CORSIA CC bridge error: RuntimeError: owned ... requires a clean pooled
connection``.

Residue is a documented state, not a contrived one: ``_NoOpClose.close()`` says
pooled connections "can retain implicit DML transactions after an exception", and
sqlite3's default deferred mode opens one on any bare write. A frame that wrote,
raised between DML and commit, and returned without releasing leaves it
permanently, because the pooled slot is never closed.
"""
from __future__ import annotations

import pytest

from subagents.cc_runtime_events import _make_on_event
from subagents.cc_runtime_state import _Live
from tests.cc_runtime_fixtures import _seed_parent

pytest_plugins = ["tests.cc_runtime_fixtures"]


def _seed(db, sid, turn_id):
    from infra.thread_driver import admit_input

    _seed_parent(db, sid=sid)
    admission = admit_input(
        sid,
        role="user",
        content="repro",
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch="boot",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    return admission.batch.identity


def _leave_residue(sid):
    """Write without committing or releasing, exactly as a raise mid-write does."""
    from infra.database import get_db

    get_db().execute("UPDATE threads SET title='dirty' WHERE id=?", (sid,))


@pytest.mark.asyncio
async def test_owned_pump_event_survives_foreign_pooled_residue(child_db):
    """An owned CC event persists its Assistant Step despite orphaned residue.

    Fails before the fix with the bridge RuntimeError; passes after, and the row
    it was supposed to write is durably there.
    """
    from infra.database import get_db

    sid, turn_id = "cc-repro", "turn-repro"
    identity = _seed(child_db, sid, turn_id)
    live = _Live(
        session=None, parent_thread_id=None, name="cc", model="opus",
        corsia_turn=identity, turn_active=True,
    )
    # Production wiring: the pump owns its own persistence (db=None).
    on_event = _make_on_event(live, sid, lambda *_a: None, None)

    _leave_residue(sid)
    assert get_db().in_transaction is True, "precondition: residue is present"

    await on_event({
        "kind": "assistant_step",
        "text": "hello",
        "reasoning": "",
        "tool_uses": [],
        "_corsia_turn_identity": identity,
    })

    rows = child_db.execute(
        "SELECT content FROM messages WHERE thread_id=? AND role='assistant'",
        (sid,),
    ).fetchall()
    assert [row["content"] for row in rows] == ["hello"]
    assert live.turn_has_assistant_row is True


@pytest.mark.asyncio
async def test_pump_leaves_no_residue_of_its_own(child_db):
    """After an owned event, the pooled connection is clean for the next borrower."""
    from infra.database import get_db

    sid, turn_id = "cc-clean", "turn-clean"
    identity = _seed(child_db, sid, turn_id)
    live = _Live(
        session=None, parent_thread_id=None, name="cc", model="opus",
        corsia_turn=identity, turn_active=True,
    )
    on_event = _make_on_event(live, sid, lambda *_a: None, None)

    await on_event({
        "kind": "assistant_step",
        "text": "one",
        "reasoning": "",
        "tool_uses": [],
        "_corsia_turn_identity": identity,
    })

    assert get_db().in_transaction is False


@pytest.mark.asyncio
async def test_a_supplied_transaction_is_still_joined_not_released(child_db):
    """When a caller supplies db, the pump joins it and must NOT roll it back.

    The release path is only for the owned wiring. A supplied connection names an
    owner the pump can see, so its open transaction is that owner's to commit.
    """
    sid, turn_id = "cc-joined", "turn-joined"
    identity = _seed(child_db, sid, turn_id)
    live = _Live(
        session=None, parent_thread_id=None, name="cc", model="opus",
        corsia_turn=identity, turn_active=True,
    )
    on_event = _make_on_event(live, sid, lambda *_a: None, child_db)

    child_db.execute("BEGIN IMMEDIATE")
    child_db.execute("UPDATE threads SET title='owner-work' WHERE id=?", (sid,))

    await on_event({
        "kind": "assistant_step",
        "text": "joined",
        "reasoning": "",
        "tool_uses": [],
        "_corsia_turn_identity": identity,
    })

    # The caller's transaction survived the event and still owns its own write.
    assert child_db.in_transaction is True
    title = child_db.execute(
        "SELECT title FROM threads WHERE id=?", (sid,)
    ).fetchone()["title"]
    assert title == "owner-work"
    child_db.commit()
