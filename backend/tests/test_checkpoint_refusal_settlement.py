"""A deterministic checkpoint refusal fails its input visibly (D3).

Holding requires a real owner and a reachable release. Only a preparing model
switch has one, so every other refusal now fails those exact inputs and lets the
rest of the FIFO through.
"""

from __future__ import annotations

import pytest

from core.checkpoint_types import CheckpointUnavailable
from infra.database import get_db
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input, finish_turn


@pytest.fixture
def refused_input(isolated_db):
    db = get_db()
    db.execute(
        "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) "
        "VALUES ('local/held','local','held','local/held','Held',100000,'t0','t0')"
    )
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('held-thread','user-1','local/held','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "held-thread", role="user", content="answer me",
        intent="apply_now", message_id="held-input", turn_id="held-turn",
        boot_epoch="boot", admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id="held-attempt", started_at="t2", db=db)
    try:
        yield db
    finally:
        db.close()


def _refuse(db, *, installed=None, window=100_000, attempt="held-attempt"):
    settle_batch_write(
        attempt, outcome="proved_no_write", db=db,
        error=CheckpointUnavailable(
            "minimum meaningful cold checkpoint input does not fit",
            context_window=window, installed_checkpoint_id=installed,
        ),
    )


def _state(db, message_id="held-input"):
    row = db.execute(
        "SELECT delivery_state,write_outcome,delivery_error,seq FROM messages "
        "WHERE id=?", (message_id,),
    ).fetchone()
    return row["delivery_state"], row["write_outcome"], row["delivery_error"], row["seq"]


def test_deterministic_refusal_fails_the_exact_input_visibly(refused_input):
    """No install, cap change or transfer is coming, so it must not wait."""
    db = refused_input
    _refuse(db)

    state, outcome, error, seq = _state(db)

    assert state == "failed"
    assert outcome == "proved_no_write"
    assert "does not fit" in error
    assert seq is not None  # A visible failure takes its place in the transcript.


def test_a_preparing_switch_still_holds_its_transferable_suffix(refused_input):
    """The switch's terminal settlement is the release this hold waits for."""
    db = refused_input
    db.execute(
        "UPDATE threads SET switch_status='preparing',pending_model='local/held',"
        "pending_execution_epoch=1,switch_after_input_order=0 WHERE id='held-thread'"
    )
    db.commit()
    _refuse(db)

    state, outcome, _error, seq = _state(db)

    assert (state, outcome, seq) == ("queued", "proved_no_write", None)


def test_an_actual_install_race_still_releases_the_input(refused_input):
    """A checkpoint that landed during the attempt is a real recovery."""
    db = refused_input
    _refuse(db, installed="a-checkpoint-that-is-not-installed")

    state, outcome, error, seq = _state(db)

    assert (state, outcome, error, seq) == ("queued", None, None, None)


def test_a_cap_change_race_still_releases_the_input(refused_input):
    """The refused fold is not the fold that would run under the new cap."""
    db = refused_input
    _refuse(db, window=50_000)

    assert _state(db) == ("queued", None, None, None)


def test_the_next_input_is_answered_after_a_visible_failure(refused_input):
    """One impossible fold must not silence the thread.

    This is the specimen shape: the later input arrives while the refused Turn
    is still open, so it queues behind the row the refusal settled. Under the
    old hold it stayed behind that row forever; the visible failure lets the
    successor Turn claim it.
    """
    db = refused_input
    _refuse(db)
    queued = admit_input(
        "held-thread", role="user", content="second ask", intent="apply_now",
        message_id="second-input", turn_id="second-turn", boot_epoch="boot",
        admitted_at="t3", db=db,
    )
    assert queued.batch is None  # The refused Turn still owns the thread.

    terminal = finish_turn(
        "held-turn", outcome="error", ended_at="t4", successor_turn_id="second-turn",
        boot_epoch="boot", db=db,
    )

    assert terminal.successor is not None
    claimed = [item.message_id for item in terminal.successor.items]
    assert claimed[-1] == "second-input"
    assert "held-input" not in claimed  # The failure is settled, not redelivered.


def test_a_held_switch_suffix_does_not_block_a_later_send(refused_input):
    """Holding stays narrow: it conserves the suffix the switch will transfer."""
    db = refused_input
    db.execute(
        "UPDATE threads SET switch_status='preparing',pending_model='local/held',"
        "pending_execution_epoch=1,switch_after_input_order=0 WHERE id='held-thread'"
    )
    db.commit()
    _refuse(db)

    assert _state(db)[0] == "queued"
    # The transfer owner, not a timer, is what moves this row.
    assert db.execute(
        "SELECT switch_status FROM threads WHERE id='held-thread'",
    ).fetchone()[0] == "preparing"


def test_turn_terminal_refusal_uses_the_same_classification(isolated_db):
    """settle_unwritten_turn_inputs must not keep its own hold policy."""
    from infra.input_writes import settle_unwritten_turn_inputs

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('turn-thread','user-1','local/held','t0','t0')"
        )
        db.commit()
        admission = admit_input(
            "turn-thread", role="user", content="terminal ask", intent="apply_now",
            message_id="turn-input", turn_id="turn-1", boot_epoch="boot",
            admitted_at="t1", db=db,
        )
        assert admission.batch is not None
        settle_unwritten_turn_inputs(
            db, "turn-1", outcome="error",
            error=CheckpointUnavailable(
                "checkpoint plus tail does not fit", context_window=100_000,
                installed_checkpoint_id=None,
            ),
        )

        assert _state(db, "turn-input")[0] == "failed"
    finally:
        db.close()
