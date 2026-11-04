"""`next_step` on terminal-error rows is true for what the row's Turn and the thread did since."""

from __future__ import annotations

import json
from contextlib import closing, nullcontext

import pytest

from infra.queries import insert_message
from infra.send_broadcast import read_input_broadcast_row
from infra.turn_errors import apply_error_next_steps, insert_turn_error, last_turn_error_id
from tests.test_turn_errors import _db, _queued_input, _turn

OLD = "2000-01-01T00:00:01"
LATER = "2000-01-02T00:00:00"


def _row(db, message_id, *, turn_id="turn-1", created_at=OLD, **metadata) -> dict:
    metadata.setdefault("type", "llm_error")
    insert_turn_error(
        insert_message, "t", "boom", metadata, turn_id=turn_id, msg_id=message_id, db=db,
        created_at=created_at,
    )
    return {"id": message_id, "metadata": metadata}


def _step(db, row) -> object:
    apply_error_next_steps(db, "t", [row])
    return row["metadata"].get("next_step", "no key")


@pytest.mark.parametrize(
    ("status", "queued", "expected"),
    [
        ("running", False, "no key"),
        ("running", True, "no key"),
        ("error", True, "held"),
        ("error", False, "failed"),
        ("completed", True, "answered"),
        ("cancelled", False, "answered"),
    ],
)
def test_an_error_of_the_latest_turn_reads_that_turns_outcome(status, queued, expected):
    with closing(_db()) as db:
        _turn(db, "turn-1", status=status, started_at="2000-01-01T00:00:00")
        row = _row(db, "error")
        if queued:
            _queued_input(db)

        assert _step(db, row) == expected


def test_an_error_of_a_turn_waiting_to_retry_reads_no_next_step():
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        db.execute("UPDATE active_turns SET status='waiting_retry', retry_at='x', ended_at=NULL")

        assert _step(db, _row(db, "error")) == "no key"


def test_a_stale_next_step_is_cleared_while_the_turn_runs():
    with closing(_db()) as db:
        _turn(db, "turn-1", status="running", started_at="2000-01-01T00:00:00")
        row = _row(db, "error")
        row["metadata"]["next_step"] = "failed"

        assert _step(db, row) == "no key"


@pytest.mark.parametrize(
    ("turn_id", "created_at", "expected"),
    [
        ("turn-1", OLD, "answered"),
        (None, OLD, "answered"),
        (None, "2000-01-03T00:00:00", "failed"),
        ("turn-1", "2000-01-02T00:00:00.9+00:00", "answered"),
    ],
)
def test_an_error_older_than_the_latest_turn_reads_by_when_it_was_written(
    turn_id, created_at, expected,
):
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        _turn(db, "turn-2", status="running", started_at=LATER)
        row = _row(db, "error", turn_id=turn_id, created_at=created_at)

        assert _step(db, row) == expected


def test_an_error_with_no_turn_reads_failed_and_a_recovered_one_stays_recovered():
    with closing(_db()) as db:
        assert _step(db, _row(db, "none", turn_id=None)) == "failed"
        _turn(db, "turn-1", status="completed", started_at=LATER)
        assert _step(db, _row(db, "recovered", recovered=True)) == "recovered"


def test_the_latest_turn_is_read_once_per_call():
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        rows = [_row(db, f"e{index}", turn_id=None) for index in range(3)]
        statements: list[str] = []
        db.set_trace_callback(statements.append)

        apply_error_next_steps(db, "t", rows)

        assert sum("FROM active_turns" in statement for statement in statements) == 1


def test_a_failed_provider_result_gets_a_next_step_and_a_stop_does_not():
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        failed = _row(db, "failed", type="result", is_error=True)
        _queued_input(db)
        stopped = {"id": "stop", "metadata": {"type": "result", "is_error": True, "stop_reason": "interrupted"}}

        assert _step(db, failed) == "held"
        assert _step(db, stopped) == "no key"


@pytest.mark.parametrize(
    ("metadata", "found"),
    [
        ({"type": "result", "is_error": True}, True),
        ({"type": "result", "is_error": True, "stop_reason": "error"}, True),
        ({"type": "result", "is_error": True, "stop_reason": "interrupted"}, False),
        ({"type": "result", "is_error": False}, False),
        ({"type": "turn_cancelled"}, False),
    ],
)
def test_the_held_row_lookup_counts_the_rows_that_need_a_reference(metadata, found):
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        metadata = {**metadata, "turn_id": "turn-1"}
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES ('row','t','system','x',?,'2000-01-01T00:00:01',1)",
            (json.dumps(metadata),),
        )

        assert last_turn_error_id(db, "t") == ("row" if found else None)


def test_the_live_error_frame_carries_no_next_step_and_settlement_projects_the_real_one():
    from core.turn_error_surface import persist_llm_error

    with closing(_db()) as db:
        _turn(db, "turn-live", status="running", started_at="2000-01-01T00:00:00")
        frame = persist_llm_error(
            lambda *_args, **_kwargs: nullcontext(db), insert_message, "t", "model",
            "turn-live", "native", "2000-01-01T00:00:01", "boom", {"cause_kind": "unknown"},
        )
        assert "next_step" not in frame
        assert "next_step" not in read_input_broadcast_row("native", db=db).metadata

        db.execute("UPDATE active_turns SET status='error' WHERE id='turn-live'")
        _queued_input(db)
        assert read_input_broadcast_row("native", db=db).metadata["next_step"] == "held"


def test_a_claim_republishes_the_error_it_made_stale(isolated_db):
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from vendor_account_fixtures import signed_in_default

    with get_db() as db:
        signed_in_default("claude-code", models=("claude-code/sonnet",), db=db)
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('t','user-1','claude-code/sonnet','x','x')"
        )
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        _row(db, "error", created_at=OLD)
        db.commit()
        admitted = admit_input(
            "t", role="user", content="try again", intent="apply_now", message_id="again",
            metadata={}, turn_id="turn-2", boot_epoch="test-boot", admitted_at=LATER, db=db,
        )
        batch = admitted.batch

        assert batch is not None and batch.error_message_ids == ("error",)
        assert read_input_broadcast_row("error", db=db).metadata["next_step"] == "answered"


def _message(db, message_id: str, role: str, seq: int, *, turn_id=None, **metadata) -> None:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq,active_turn_id) "
        "VALUES (?,'t',?,'x',?,'2000-01-01T00:00:00',?,?)",
        (message_id, role, json.dumps(metadata), seq, turn_id),
    )


@pytest.mark.parametrize(
    ("nudged", "shown", "queued", "expected"),
    [
        (True, True, False, "answered"),
        (True, False, False, "failed"),
        (False, True, False, "failed"),
        (True, True, True, "held"),
    ],
)
def test_a_failed_nudge_turn_reads_answered_once_the_persons_reply_shows(
    nudged, shown, queued, expected,
):
    with closing(_db()) as db:
        _turn(db, "turn-1", started_at="2000-01-01T00:00:00")
        row = _row(db, "error")
        _message(db, "person", "user", 10)
        _message(db, "earlier", "assistant", 11, for_user=shown)
        metadata = {"kind": "reply_not_visible", "person_message_id": "person"} if nudged else {}
        _message(db, "nudge", "user", 12, turn_id="turn-1", **metadata)
        if queued:
            _queued_input(db)

        assert _step(db, row) == expected
