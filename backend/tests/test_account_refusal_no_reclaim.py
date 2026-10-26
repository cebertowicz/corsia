"""An account refusal at launch holds its input; the terminal claim never re-claims it (#1493)."""

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
