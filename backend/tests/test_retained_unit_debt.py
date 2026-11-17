"""A refused retained tool result is one unit of named debt, not a wedged Turn.

Contract (Turn & Delivery ruling on bd9826d8): each retained unit in
``persist_completed_tool_results_in_transaction`` runs inside the existing
``settled_or_debt``. A refused unit is rolled back, logged once by name and
omitted from the return; the terminal still applies and closes the call with
one durable missing-result row (``turn_ended_without_result``), so a later cold
checkpoint finds no debt. The refused bytes are never stored. Only a lost
savepoint still raises.

The first three cases held before each unit was settled on its own; in the
rest, the bare retained write (``infra/thread_driver.py`` ->
``infra/completed_tool_results.py``) used to raise and leave the Turn running.
"""
from __future__ import annotations

import pytest
from loguru import logger

from core._tool_result_delivery_support import ToolResultBindingError
from infra.delivery_states import (
    PROVIDER_MISSING_RESULT_REASON,
    TURN_ENDED_WITHOUT_RESULT_REASON,
)
from core.turn_core import BOOT_EPOCH, persist_turn_settlement, persist_turn_terminal
from history.projection import render_portable_history
from infra.completed_tool_results import persist_completed_tool_results
from infra.thread_driver import TerminalResult, admit_input
from infra.thread_input_types import ClaimedBatch
from subagents.child_thread import persist_assistant_step, persist_tool_result
from tests import native_tool_result_lab as tray
from tools.recorded_call import CompletedToolCall, CompletedToolFailure

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

REAL = "REAL-RESULT-BYTES-OF-THE-REFUSED-UNIT"
KEPT = "REAL-RESULT-BYTES-OF-THE-PERSISTED-UNIT"
SAME = {"command": "true"}
OTHER = {"command": "false"}


@pytest.fixture
def lab(tmp_path, monkeypatch, isolated_db):
    lab = tray.install(tmp_path, monkeypatch)
    yield lab
    lab.db.close()


@pytest.fixture
def errors():
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="ERROR")
    yield seen
    logger.remove(sink)


def _declare(lab, *calls):
    step = persist_assistant_step(
        "thread-1", content="", turn_id=lab.identity.turn_id, db=lab.db,
        tool_uses=[{"id": cid, "name": "bash", "arguments": args} for cid, args in calls],
    )
    lab.db.commit()
    return step


def _unit(lab, step, call_id, arguments, result):
    return CompletedToolCall(
        name="bash", result=result, arguments=arguments, thread_id="thread-1",
        turn_id=lab.identity.turn_id, invocation_id=call_id,
        provider_tool_call_id=call_id, assistant_step_id=step,
    )


def _turn(lab):
    row = lab.db.execute(
        "SELECT status,error FROM active_turns WHERE id=?", (lab.identity.turn_id,),
    ).fetchone()
    return row["status"], row["error"]


def _debt(errors):
    return [line for line in errors if "stays unpaid debt" in line]


def _projected_tool_messages(lab):
    projection = render_portable_history("thread-1", db=lab.db)
    return [
        message for turn in projection.turns for message in turn.messages
        if message.role == "tool"
    ]


def _terminal_close(lab):
    """The one row the terminal wrote for a call it left open; refused bytes absent."""
    [(row_id, content, meta)] = lab.tool_rows()
    assert meta["synthetic_reason"] == TURN_ENDED_WITHOUT_RESULT_REASON
    assert meta["synthetic_tool_result"] is True and REAL not in content
    return row_id, meta


def _duplicate_failure(lab):
    """One Step declaring ``dup-1`` twice, and both completed units retained."""
    step = _declare(lab, ("dup-1", SAME), ("dup-1", SAME))
    return step, CompletedToolFailure((
        _unit(lab, step, "dup-1", SAME, REAL), _unit(lab, step, "dup-1", SAME, REAL),
    ))


# --- controls -------------------------------------------------------------


def test_terminal_writes_one_durable_close_and_projects_it(lab):
    """CONTROL: an unanswered declared call ends with one durable close."""
    step = _declare(lab, ("unanswered-1", SAME))

    assert persist_turn_terminal(
        lab.identity.turn_id, "error", "provider failed", db=lab.db,
    ) is TerminalResult.APPLIED
    row_id, meta = _terminal_close(lab)
    assert meta["assistant_step_id"] == step and meta["tool_call_id"] == "unanswered-1"
    [projected] = _projected_tool_messages(lab)
    assert projected.message_id == row_id
    assert projected.synthetic_reason == TURN_ENDED_WITHOUT_RESULT_REASON


def test_duplicate_declaration_gets_one_close_for_the_repeated_id(lab):
    """A Step declaring one id twice gets ONE durable close, projected once."""
    step = _declare(lab, ("dup-1", SAME), ("dup-1", SAME))
    persist_turn_terminal(lab.identity.turn_id, "error", "provider failed", db=lab.db)

    row_id, meta = _terminal_close(lab)
    assert meta["assistant_step_id"] == step and meta["tool_call_id"] == "dup-1"
    [projected] = _projected_tool_messages(lab)
    assert projected.message_id == row_id


def test_terminal_lost_savepoint_raises_and_the_turn_stays_running(lab, monkeypatch):
    """CONTROL: a transaction lost inside a retained unit still fails the terminal.

    On the base this passes because every refusal raises; it becomes the
    distinguishing case once refusals degrade to debt.
    """
    step, failure = _duplicate_failure(lab)

    def loses_the_transaction(db, **_kwargs):
        db.execute("ROLLBACK")
        raise ToolResultBindingError("refused after the transaction was lost")

    monkeypatch.setattr(
        "infra.completed_tool_results._persist_one_completed_tool_result",
        loses_the_transaction,
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 - the driver's own error type is not pinned
        persist_turn_terminal(lab.identity.turn_id, "error", failure, db=lab.db)
    assert _turn(lab)[0] == "running"
    assert lab.tool_rows() == []


# --- refused units -----------------------------------------------------------


def test_duplicate_id_refusals_are_named_debt_and_the_terminal_applies(lab, errors):
    """(a) APPLIED, error, successor claimed, one close, one line per unit."""
    step, failure = _duplicate_failure(lab)
    admit_input(
        "thread-1", role="user", content="next", intent="apply_now",
        message_id="queued-1", turn_id="turn-queued", boot_epoch=BOOT_EPOCH,
        admitted_at="t1", db=lab.db,
    )

    settlement = persist_turn_settlement(lab.identity.turn_id, "error", failure, db=lab.db)

    assert settlement.result is TerminalResult.APPLIED
    status, error = _turn(lab)
    assert status == "error" and REAL not in (error or "")
    assert isinstance(settlement.successor, ClaimedBatch)
    row_id, _meta = _terminal_close(lab)
    debt = _debt(errors)
    assert len(debt) == 2
    for line in debt:
        assert "dup-1" in line and lab.identity.turn_id in line and "thread-1" in line
        assert "ToolResultBindingError" in line and REAL not in line
    [projected] = _projected_tool_messages(lab)
    assert projected.message_id == row_id


def test_mixed_batch_keeps_the_persisted_unit_through_the_commit(lab, errors):
    """(b) unit 1 refused and closed by the terminal; unit 2 persists and survives."""
    step = _declare(lab, ("dup-1", SAME), ("dup-1", SAME), ("other-2", OTHER))
    failure = CompletedToolFailure((
        _unit(lab, step, "dup-1", SAME, REAL), _unit(lab, step, "other-2", OTHER, KEPT),
    ))

    assert persist_turn_terminal(
        lab.identity.turn_id, "error", failure, db=lab.db,
    ) is TerminalResult.APPLIED
    lab.db.rollback()  # nothing uncommitted may be holding the row
    rows = {meta["tool_call_id"]: (content, meta) for _row_id, content, meta in lab.tool_rows()}
    assert sorted(rows) == ["dup-1", "other-2"]
    assert rows["dup-1"][1]["synthetic_reason"] == TURN_ENDED_WITHOUT_RESULT_REASON
    assert REAL not in rows["dup-1"][0]
    content, meta = rows["other-2"]
    assert content == KEPT and meta["tool_call_id"] == "other-2"
    assert meta["tool_invocation_id"] == "other-2" and "synthetic_tool_result" not in meta
    [line] = _debt(errors)
    assert "dup-1" in line and KEPT not in line


def test_non_terminal_entry_returns_only_the_persisted_results(lab, errors):
    """(d) the pre-terminal entry omits the refused unit and does not raise."""
    step = _declare(lab, ("dup-1", SAME), ("dup-1", SAME), ("other-2", OTHER))
    failure = CompletedToolFailure((
        _unit(lab, step, "dup-1", SAME, REAL), _unit(lab, step, "other-2", OTHER, KEPT),
    ))

    persisted = persist_completed_tool_results(lab.identity.turn_id, failure, db=lab.db)

    assert [result.content for result in persisted] == [KEPT]
    assert _turn(lab)[0] == "running"
    assert len(_debt(errors)) == 1


def test_harness_settlement_then_second_retained_attempt_leaves_one_row(
    lab, errors, monkeypatch,
):
    """(e) refused unit, harness missing-result close, terminal refuses again.

    The close is written by ``persist_tool_result``, the one writer both
    ``cc_event_tool_settlement._settle_owner`` and
    ``codex_runtime_events.settle_missing_tool_results`` call; the live harness
    objects around it are not driven here.
    """
    step = _declare(lab, ("call-1", SAME))
    failure = CompletedToolFailure((_unit(lab, step, "call-1", SAME, REAL),))
    with monkeypatch.context() as first_attempt:
        def refuse(**_kwargs):
            raise ToolResultBindingError("first retained write refused for the lab")

        first_attempt.setattr("core.tool_result_delivery._persist_resolved_result", refuse)
        assert persist_completed_tool_results(
            lab.identity.turn_id, failure, db=lab.db,
        ) == ()
    assert lab.tool_rows() == []

    close_id = persist_tool_result(
        "thread-1", turn_id=lab.identity.turn_id, tool_use_id="call-1", name="bash",
        content="missing", is_error=True, assistant_step_id=step,
        synthetic_tool_result=True, synthetic_reason=PROVIDER_MISSING_RESULT_REASON,
        db=lab.db,
    )

    assert persist_turn_terminal(
        lab.identity.turn_id, "error", failure, db=lab.db,
    ) is TerminalResult.APPLIED
    [(row_id, content, meta)] = lab.tool_rows()
    assert row_id == close_id and content == "missing"
    assert meta["synthetic_reason"] == PROVIDER_MISSING_RESULT_REASON
    assert len(_debt(errors)) == 2
    assert all(REAL not in line for line in errors)
    [projected] = _projected_tool_messages(lab)
    assert projected.message_id == close_id
