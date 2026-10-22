"""A failed model switch must not strand the inputs it retargeted (D4).

A switch that never runs leaves its destination epoch ownerless: the
destination will not answer those inputs and the source no longer selects
them, so the thread goes silent with the inputs still queued. Failing the
switch returns every wholly unstarted input to the source, and new inputs stop
routing to a destination that failed.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from infra.input_admission import accept_thread_input
from infra.model_switches import fail_switch, request_switch
from infra.thread_inputs import start_idle_turn

SOURCE = "local/source"
DESTINATION = "native/destination"


def _model(db, model_id):
    db.execute(
        "INSERT INTO models (id,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) "
        "VALUES (?,'local',?,?,?,200000,'t0','t0')",
        (model_id, model_id, model_id, model_id),
    )


@pytest.fixture
def failed_switch(isolated_db):
    """A switch that failed after retargeting two unstarted inputs."""
    db = get_db()
    _model(db, SOURCE)
    _model(db, DESTINATION)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('switch-thread','user-1',?,'t0','t0')", (SOURCE,),
    )
    db.commit()
    accept_thread_input(
        "switch-thread", role="user", content="before the switch",
        intent="apply_now", message_id="input-1", db=db,
    )
    request_switch(
        "switch-thread", DESTINATION, operation_id="switch-1",
        requested_at="t1", db=db,
    )
    for index in (2, 3):
        accept_thread_input(
            "switch-thread", role="user", content=f"queued {index}",
            intent="apply_now", message_id=f"input-{index}", db=db,
        )
    try:
        yield db
    finally:
        db.close()


def _rows(db):
    return [
        (row["id"], row["input_order"], row["target_execution_epoch"], row["target_model"])
        for row in db.execute(
            "SELECT id,input_order,target_execution_epoch,target_model FROM messages "
            "WHERE input_order IS NOT NULL ORDER BY input_order",
        )
    ]


def test_retargeted_inputs_start_out_on_the_failed_destination(failed_switch):
    """The trap this fixes: the switch owns inputs the source will not answer."""
    assert _rows(failed_switch) == [
        ("input-1", 1, 0, SOURCE),
        ("input-2", 2, 1, DESTINATION),
        ("input-3", 3, 1, DESTINATION),
    ]


def test_failed_switch_returns_unstarted_inputs_to_the_source(failed_switch):
    """Every wholly unstarted input goes back, with its FIFO order untouched."""
    db = failed_switch

    fail_switch("switch-1", error="minimum meaningful cold fork input does not fit", db=db)

    assert _rows(db) == [
        ("input-1", 1, 0, SOURCE),
        ("input-2", 2, 0, SOURCE),
        ("input-3", 3, 0, SOURCE),
    ]


def test_the_failed_switch_stays_visible_after_the_return(failed_switch):
    """Returning the inputs is not the same as discarding the user's request."""
    db = failed_switch

    fail_switch("switch-1", error="cold checkpoint does not fit", db=db)

    assert tuple(db.execute(
        "SELECT switch_status,switch_error,pending_model FROM threads WHERE id='switch-thread'",
    ).fetchone()) == ("failed", "cold checkpoint does not fit", DESTINATION)


UNCERTAIN = (
    ("write_started_at", "in-flight"),
    ("active_turn_id", "claimed-elsewhere"),
    ("write_outcome", "ambiguous"),
    ("write_outcome", "accepted"),
    ("seq", 400),
    ("delivery_attempt_id", "unknown-attempt"),
)


@pytest.mark.parametrize("column,value", UNCERTAIN)
def test_an_uncertain_head_quarantines_everything_behind_it(
    failed_switch, column, value,
):
    """Nothing may overtake a row that may already own a provider outcome."""
    db = failed_switch
    db.execute(f"UPDATE messages SET {column}=? WHERE id='input-2'", (value,))
    db.commit()
    before = _rows(db)

    fail_switch("switch-1", error="cold checkpoint does not fit", db=db)

    assert _rows(db) == before


@pytest.mark.parametrize("column,value", UNCERTAIN)
def test_the_clean_prefix_returns_and_the_uncertain_row_stays(
    failed_switch, column, value,
):
    """Recovery goes as far as it provably can, and stops exactly there."""
    db = failed_switch
    db.execute(f"UPDATE messages SET {column}=? WHERE id='input-3'", (value,))
    db.commit()

    fail_switch("switch-1", error="cold checkpoint does not fit", db=db)

    assert _rows(db) == [
        ("input-1", 1, 0, SOURCE),
        ("input-2", 2, 0, SOURCE),
        ("input-3", 3, 1, DESTINATION),
    ]


def test_a_new_input_never_routes_to_a_failed_destination(failed_switch):
    """The destination cannot answer anything, so nothing may be sent to it."""
    db = failed_switch
    fail_switch("switch-1", error="cold checkpoint does not fit", db=db)

    accepted = accept_thread_input(
        "switch-thread", role="user", content="after the failure",
        intent="apply_now", message_id="input-4", db=db,
    )

    assert (accepted.target_model, accepted.target_execution_epoch) == (SOURCE, 0)


def test_the_source_answers_the_returned_inputs_in_fifo_order(failed_switch):
    """The composed result: the thread speaks again, in the order it was asked."""
    db = failed_switch
    fail_switch("switch-1", error="cold checkpoint does not fit", db=db)
    accept_thread_input(
        "switch-thread", role="user", content="after the failure",
        intent="apply_now", message_id="input-4", db=db,
    )

    batch = start_idle_turn(
        "switch-thread", turn_id="recovered-turn", boot_epoch="boot",
        started_at="t9", db=db,
    )

    assert batch is not None
    assert batch.target_model == SOURCE
    assert [item.message_id for item in batch.items] == [
        "input-1", "input-2", "input-3", "input-4",
    ]
