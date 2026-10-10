"""All three guarded persistence sites are reachable from an owned CC event.

The brief for this fix named three, not two, and they fail in different helpers:

* ``infra/queries.py`` ``insert_message`` -- reached by ``_on_assistant_step``
  BEFORE the replay helper's guard, so it is the one production actually hits;
* ``subagents/child_thread.py`` ``_insert_message_once`` -- the replay guard,
  reached when the Assistant Step carries a tool batch (a replay-stable id);
* ``subagents/child_thread.py`` ``persist_tool_result`` -- reached through
  ``settle_missing_tool_results``.

Each is verified here directly against residue, so a future change that reopens
one of them fails on the specific site rather than on a general symptom.
"""
from __future__ import annotations

import pytest

from subagents.child_thread import _insert_message_once, persist_tool_result
from tests.cc_runtime_fixtures import _seed_parent

pytest_plugins = ["tests.cc_runtime_fixtures"]


def _residue(sid):
    from infra.database import get_db

    get_db().execute("UPDATE threads SET title='dirty' WHERE id=?", (sid,))


def test_guard_1_insert_message_still_refuses_a_foreign_transaction(child_db):
    """The guards are NOT weakened: an owned claim on a dirty connection refuses."""
    from infra.queries import insert_message

    _seed_parent(child_db, sid="g1")
    child_db.commit()
    _residue("g1")

    with pytest.raises(RuntimeError, match="claimed the outermost"):
        insert_message("g1", "assistant", "x", {"turn_unbound_reason": "legacy_import"})


def test_guard_2_replay_insert_still_refuses_a_foreign_transaction(child_db):
    _seed_parent(child_db, sid="g2")
    child_db.commit()
    _residue("g2")

    with pytest.raises(RuntimeError, match="clean pooled connection"):
        _insert_message_once(
            "g2", "assistant", "x",
            {"turn_unbound_reason": "legacy_import"},
            msg_id="mas_probe",
        )


def test_guard_3_persist_tool_result_still_refuses_a_foreign_transaction(child_db):
    _seed_parent(child_db, sid="g3")
    child_db.commit()
    _residue("g3")

    with pytest.raises(RuntimeError, match="clean pooled connection"):
        persist_tool_result(
            "g3", turn_id="t-g3", tool_use_id="tu-1", name="read_file",
            content="x", assistant_step_id="step-1",
        )


@pytest.mark.asyncio
async def test_settle_missing_tool_results_survives_residue_under_owned_pump(child_db):
    """Guard 3's caller: an interrupted turn stubs its owed results despite residue.

    ``_on_assistant_step`` reaches ``settle_missing_tool_results`` when the turn is
    interrupt-requested, and that path persists synthetic tool rows through the
    third guarded site.
    """
    from subagents.cc_runtime_events import _make_on_event
    from subagents.cc_runtime_state import _Live
    from infra.thread_driver import admit_input

    sid, turn_id = "cc-settle", "turn-settle"
    _seed_parent(child_db, sid=sid)
    admission = admit_input(
        sid,
        role="user",
        content="calling a tool",
        intent="apply_now",
        message_id="input-settle",
        turn_id=turn_id,
        boot_epoch="boot",
        admitted_at="t",
        db=child_db,
    )
    assert admission.batch is not None
    identity = admission.batch.identity
    live = _Live(
        session=None, parent_thread_id=None, name="cc", model="opus",
        corsia_turn=identity, turn_active=True,
    )
    live.interrupt_requested = True
    on_event = _make_on_event(live, sid, lambda *_a: None, None)

    _residue(sid)

    await on_event({
        "kind": "assistant_step",
        "text": "calling a tool",
        "reasoning": "",
        "tool_uses": [{
            "id": "toolu_1",
            "name": "mcp__corsia__read_file",
            "input": {"path": "x"},
        }],
        "_corsia_turn_identity": identity,
    })

    tool_rows = child_db.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND role='tool'",
        (sid,),
    ).fetchone()["n"]
    assert tool_rows == 1, "the interrupted turn's owed result was stubbed durably"
