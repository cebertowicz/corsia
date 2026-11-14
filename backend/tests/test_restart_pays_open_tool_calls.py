"""A Turn that a restart ends still gives every call it declared a durable result.

The harness that would have settled an open call died with the old process, so
restart recovery's terminal is the only writer left. Without a durable row the
call stays debt, and every later cold checkpoint over it is refused.
"""
from __future__ import annotations

import json

import pytest

from infra.thread_recovery import recover_threads
from test_cold_install_floor_debt import _capture_and_install
from test_external_cold_tool_debt import _context, debt_db  # noqa: F401


def _declare_older_twice(db) -> None:
    row = db.execute("SELECT metadata FROM messages WHERE id='older-step'").fetchone()
    metadata = json.loads(row["metadata"])
    metadata["llm_tool_calls"].append(dict(metadata["llm_tool_calls"][0]))
    db.execute("UPDATE messages SET metadata=? WHERE id='older-step'", (json.dumps(metadata),))
    db.commit()


def _restart(db) -> None:
    recovered = recover_threads(
        boot_epoch="boot-2", recovered_at="t9",
        successor_id=lambda _thread_id: "unused", db=db,
    )
    assert recovered.ended_turns == (("debt", "current"),)


def _older_results(db) -> list[dict]:
    rows = db.execute(
        "SELECT metadata FROM messages WHERE thread_id='debt' AND role='tool' "
        "AND json_extract(metadata,'$.tool_call_id')='older'"
    ).fetchall()
    return [json.loads(row["metadata"]) for row in rows]


def _cold_request(boundary):
    from core.checkpoint_types import CheckpointRequest

    return CheckpointRequest(
        id="after-restart", thread_id="debt", history_epoch=boundary.history_epoch,
        previous_generation_id=None, through_seq=boundary.seq,
        through_message_id=boundary.message_id, trigger="pressure", mode="cold",
        source_harness="codex", source_model="codex/test",
        writer_harness="codex", writer_model="codex/test",
        originating_turn_id="current", first_included_seq=1, started_at="t",
    )


@pytest.mark.parametrize("declared_twice", [False, True])
def test_restart_pays_open_call_so_a_cold_checkpoint_installs(
    debt_db, monkeypatch, declared_twice,
):
    from core.tool_result_debt import thread_tool_debt_through
    from history.projection import render_portable_history
    from infra.delivery_states import TURN_ENDED_WITHOUT_RESULT_REASON

    _context(debt_db, monkeypatch, "codex", outstanding=True)
    if declared_twice:
        _declare_older_twice(debt_db)

    _restart(debt_db)

    projection = render_portable_history("debt", db=debt_db)
    boundary = projection.sealed_boundary
    assert boundary is not None
    installed = _capture_and_install(debt_db, _cold_request(boundary))
    assert (installed.status, installed.error) == ("installed", None)
    assert not thread_tool_debt_through("debt", boundary.seq, db=debt_db).unsettled

    results = _older_results(debt_db)
    assert len(results) == 1
    assert results[0]["assistant_step_id"] == "older-step"
    assert results[0]["turn_id"] == "current"
    assert results[0]["synthetic_reason"] == TURN_ENDED_WITHOUT_RESULT_REASON
    # The model is still told this result was never recorded.
    assert len(projection.missing_result_stub_seqs) == 1


def test_answered_calls_get_no_second_result(debt_db, monkeypatch):
    _context(debt_db, monkeypatch, "codex", outstanding=False)

    _restart(debt_db)

    assert [m.get("synthetic_reason") for m in _older_results(debt_db)] == [None]
    count = debt_db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='debt' AND role='tool'"
    ).fetchone()[0]
    assert count == 2
