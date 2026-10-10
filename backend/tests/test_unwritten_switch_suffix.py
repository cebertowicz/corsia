"""Only a wholly unstarted FIFO suffix may escape a checkpoint-held source.

A refusal is conserved only while a preparing switch owes it a transfer, so
these tests request the switch before the refusal settles and let the terminal
owner move the suffix. The transfer predicate itself is unchanged.
"""

import json

import pytest

from infra.input_admission import accept_thread_input
from infra.model_switches import fail_switch, request_switch
from infra.thread_driver import finish_turn
from test_checkpoint_unavailable_release import (  # noqa: F401
    _preparing_switch,
    _settle_refusal,
    blocked_input,
)


def _request(db, destination="native/destination", operation="switch-held"):
    return request_switch(
        "held-thread", destination, operation_id=operation, requested_at="t4", db=db,
    )


def _finish(db):
    return finish_turn(
        "held-turn", outcome="error", ended_at="t3", successor_turn_id="unused",
        boot_epoch="boot", db=db,
    )


def _suffix(db):
    accept_thread_input(
        "held-thread", role="user", content="healthy behind hold", intent="queue_after_turn",
        message_id="healthy-suffix", metadata={"sentinel": "keep"},
        delivery_options={"attachments": [{"id": "media-sentinel"}]}, db=db,
    )


def _conserved(db):
    """Reach the one state that still conserves an unwritten suffix.

    The suffix is accepted first so it shares the source epoch the transfer
    moves, and the switch is requested before the refusal settles because that
    pending transfer is the only owner a conserved input may wait for.
    """
    _suffix(db)
    _preparing_switch(db)
    _settle_refusal(db)


def test_mixed_suffix_moves_once_with_content_media_intent_and_order(blocked_input):
    db = blocked_input
    _conserved(db)
    before = [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE id IN ('held-input','healthy-suffix') ORDER BY input_order",
    )]
    _finish(db)  # The terminal owner performs the one transfer.
    after = [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE id IN ('held-input','healthy-suffix') ORDER BY input_order",
    )]
    for previous, current in zip(before, after, strict=True):
        for key in ("id", "input_order", "role", "content", "metadata", "delivery_options", "delivery_intent"):
            assert current[key] == previous[key]
        assert current["target_model"] == "native/destination"
        assert current["target_execution_epoch"] == 1
        assert current["delivery_revision"] == previous["delivery_revision"] + 1
        assert current["delivery_state"] == "queued"
        for key in ("seq", "active_turn_id", "write_started_at", "write_outcome", "delivery_attempt_id"):
            assert current[key] is None
    assert json.loads(after[1]["delivery_options"])["attachments"] == [{"id": "media-sentinel"}]
    _request(db)
    assert [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE id IN ('held-input','healthy-suffix') ORDER BY input_order",
    )] == after


def test_terminal_owner_releases_held_suffix_only_after_source_turn_finishes(blocked_input):
    db = blocked_input
    _conserved(db)
    assert _request(db).waiting_for_source
    assert db.execute("SELECT target_execution_epoch FROM messages WHERE id='held-input'").fetchone()[0] == 0
    settled = _finish(db)
    assert settled.successor is None  # No source-model execution after transfer.
    assert settled.switch_request is not None
    assert not settled.switch_request.waiting_for_source
    assert db.execute("SELECT target_execution_epoch FROM messages WHERE id='healthy-suffix'").fetchone()[0] == 1


@pytest.mark.parametrize("column,value", (
    ("active_turn_id", "claimed-elsewhere"),
    ("seq", 400),
    ("write_started_at", "in-flight"),
    ("write_outcome", "ambiguous"),
    ("write_outcome", "accepted"),
    ("delivery_attempt_id", "unknown-attempt"),
))
def test_uncertain_suffix_prevents_partial_retarget(blocked_input, column, value):
    db = blocked_input
    _conserved(db)
    db.execute(f"UPDATE messages SET {column}=? WHERE id='healthy-suffix'", (value,))
    db.commit()
    before = [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY rowid")]
    _finish(db)
    assert _request(db).waiting_for_source
    assert [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY rowid")] == before


def test_failed_switch_returns_unstarted_suffix_to_source(blocked_input):
    """A failed destination must not keep the FIFO it never wrote anything to."""
    db = blocked_input
    _conserved(db)
    _finish(db)  # The terminal owner transfers the suffix to the destination epoch.
    before = [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE id IN ('held-input','healthy-suffix') ORDER BY input_order",
    )]
    assert {row["target_execution_epoch"] for row in before} == {1}
    assert {row["target_model"] for row in before} == {"native/destination"}

    fail_switch("switch-held", error="destination unusable", db=db)

    after = [dict(row) for row in db.execute(
        "SELECT * FROM messages WHERE id IN ('held-input','healthy-suffix') ORDER BY input_order",
    )]
    for previous, current in zip(before, after, strict=True):
        assert current["id"] == previous["id"]
        assert current["input_order"] == previous["input_order"]
        assert current["target_execution_epoch"] == 0
        assert current["target_model"] == "local/held"
        assert current["delivery_state"] == "queued"
        assert current["delivery_revision"] == previous["delivery_revision"] + 1
        for key in ("seq", "active_turn_id", "write_started_at", "write_outcome", "delivery_attempt_id"):
            assert current[key] is None

    # A new send after the failure must never route to the failed destination.
    accept_thread_input(
        "held-thread", role="user", content="new after failure", intent="apply_now",
        message_id="new-after-failure", db=db,
    )
    new_row = db.execute(
        "SELECT target_execution_epoch,target_model FROM messages WHERE id='new-after-failure'",
    ).fetchone()
    assert (new_row["target_execution_epoch"], new_row["target_model"]) == (0, "local/held")


def test_failed_switch_never_moves_an_uncertain_row(blocked_input):
    """Only a wholly unstarted row may return; an uncertain write stays put."""
    db = blocked_input
    _conserved(db)
    _finish(db)
    db.execute(
        "UPDATE messages SET write_outcome='ambiguous' WHERE id='healthy-suffix'",
    )
    db.commit()
    fail_switch("switch-held", error="destination unusable", db=db)
    row = dict(db.execute(
        "SELECT target_execution_epoch,target_model,write_outcome FROM messages "
        "WHERE id='healthy-suffix'",
    ).fetchone())
    assert row == {
        "target_execution_epoch": 1, "target_model": "native/destination",
        "write_outcome": "ambiguous",
    }


def test_healthy_head_still_runs_on_source_before_switch(blocked_input):
    db = blocked_input
    _conserved(db)
    db.execute("UPDATE messages SET write_outcome=NULL,delivery_attempt_id=NULL,delivery_error=NULL WHERE target_execution_epoch=0")
    db.execute("UPDATE messages SET write_outcome='proved_no_write',delivery_error='checkpoint unavailable' WHERE id='healthy-suffix'")
    db.commit()
    _finish(db)
    assert _request(db).waiting_for_source
    assert set(row[0] for row in db.execute(
        "SELECT target_execution_epoch FROM messages WHERE delivery_state='queued'",
    )) == {0}
