"""An account refusal at launch holds its input; the terminal claim never re-claims it (#1493).

The claim's account check and the launch's account check can disagree (here, on the
account's home directory). These tests pin that the disagreement costs one refused
launch per trigger, never a recursive re-claim; making the two checks agree is the
hold evaluator's change, not this one.
"""

import pytest


@pytest.mark.asyncio
async def test_account_refusal_settles_once_and_holds_input(main_db, monkeypatch):
    from connections.account_policy import AccountAdmissionError
    from infra import thread_execution
    from infra.input_admission import accept_thread_input

    attempts = []

    async def refuse(*_args, **_kwargs):
        attempts.append(1)
        raise AccountAdmissionError("account_home_mismatch")

    monkeypatch.setattr(thread_execution, "_dispatch_prepared", refuse)
    accept_thread_input(
        "main-1", role="user", content="hello", intent="apply_now",
        message_id="input-1", db=main_db,
    )

    with pytest.raises(AccountAdmissionError):
        await thread_execution.drive_thread("main-1")

    assert len(attempts) == 1
    turns = main_db.execute("SELECT status FROM active_turns").fetchall()
    assert [row["status"] for row in turns] == ["error"]
    row = main_db.execute(
        "SELECT delivery_state,write_outcome,delivery_error,active_turn_id "
        "FROM messages WHERE id='input-1'"
    ).fetchone()
    assert tuple(row) == ("queued", "proved_no_write", "account_home_mismatch", None)
    assert main_db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE status='running'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("switch_ready", [False, True])
def test_account_refusal_claims_only_behind_a_switch_it_commits(isolated_db, switch_ready):
    from connections.account_policy import AccountAdmissionError
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.model_switches import request_switch
    from infra.thread_driver import finish_turn
    from infra.thread_input_types import ClaimedBatch

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread','user','claude-code/sonnet','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            "VALUES ('current','thread',0,'claude-code/sonnet','running','t','boot')",
        )
        if switch_ready:
            # Input sent after the switch belongs to its destination, so it never holds the commit.
            request_switch("thread", "claude-code/opus", operation_id="switch", requested_at="t", db=db)
            db.execute("UPDATE threads SET switch_status='ready' WHERE id='thread'")
        accept_thread_input(
            "thread", role="user", content="work", intent="apply_now", message_id="queued", db=db,
        )
        db.commit()

        settlement = finish_turn(
            "current", outcome="error", ended_at="later", successor_turn_id="next",
            boot_epoch="boot", error=AccountAdmissionError("account_home_mismatch"), db=db,
        )

        if switch_ready:
            # The committed switch changed the account, so its destination claims normally.
            assert settlement.switch_transition is not None
            assert isinstance(settlement.successor, ClaimedBatch)
            assert settlement.successor.target_model == "claude-code/opus"
        else:
            assert settlement.switch_transition is None
            assert settlement.successor is None
            assert db.execute(
                "SELECT delivery_state,active_turn_id FROM messages WHERE id='queued'",
            ).fetchone()[:] == ("queued", None)
    finally:
        db.close()
